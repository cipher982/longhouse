# Provider Automation Factory Epic

**Status:** Phases 0–2 implemented; Phase 3 remains conditional on measured churn
**Owner:** Longhouse
**Updated:** 2026-07-23
**Scope:** Continuous provider discovery, control proof, transcript normalization,
timeline presentation, regression detection, and automated remediation

## Executive decision

Longhouse will have one provider automation factory, not separate systems for
control compatibility, transcript parsing, release canaries, and timeline
polish.

For every supported provider, the factory continuously answers:

1. What can Longhouse control?
2. What evidence does the provider emit?
3. Can Longhouse preserve and normalize that evidence without loss?
4. How should it be presented to a human?
5. Did an upstream release or newly observed wire shape change any answer?

```text
provider release + authorized corpus + provider adapter + universal contracts
  -> immutable raw evidence
  -> canonical events with provenance
  -> disposable presentation
  -> behavioral and visual proof
  -> drift verdict + remediation candidate
```

The factory discovers regressions from provider releases and real Longhouse
traffic. It does not wait for a user to notice that steering stopped working,
a tool result disappeared, or a timeline became unreadable.

This is one factory with two independent verdict streams:

- **Control proof** covers launch, send, steer, interrupt, pause, resume, stop,
  identity, and runtime truth.
- **Transcript proof** covers capture, pairing, normalization, presentation,
  disclosure, concision, and cross-surface parity.

They share provider identity, adapters, evidence, scenarios, and artifacts, but
one verdict never masks or blocks an unrelated change in the other stream.

Raw evidence remains authoritative. Automation may improve a deterministic
projection; it may never rewrite history, invent intent, silently discard an
unknown event, or hide uncertainty.

### Entry proof

The first implementation step is the already-defined one Parsed Codex wrapper
rule in `tool-translation-evaluation.md` Phase 2. It must prove that Parsed
translation materially improves real sessions while preserving attribution,
failures, and raw disclosure. That experiment establishes the first structural
rule and supplies measured review cost, shape churn, and corpus impact.

Heavy machinery is earned separately:

- batch historical/release discovery starts immediately because it reuses the
  existing archive and harness;
- continuous Machine Agent telemetry requires evidence that batch discovery
  misses important drift;
- automatic fixture minimization requires fixture volume that makes assisted
  extraction a material burden; and
- candidate auto-promotion remains restricted to mechanically exact aliases.

### Implementation result

Phases 0–2 now run through one read-time presentation projector and one proof
pipeline. Raw events remain unchanged; the API adds a disposable semantic
projection consumed directly by web and iOS. Successful repetitive waits group,
failed waits remain prominent, and receded Codex wrappers retain child
provenance plus raw disclosure.

The hermetic corpus covers all five declared providers. Native Codex archive
discovery separately scans authorized history, fingerprints shapes without
retaining values, accounts for call/result pairing, and reports incomplete or
stale windows as Unknown. Hermetic evidence is explicitly synthetic and cannot
mark discovery current. The universal harness now requires executable,
per-action oracles for shape inventory, drift, conservation, projection,
grouping, disclosure, and cross-surface parity.

Phase 3 has not activated: the measured native Codex unknown rate is small
enough that automatic rule mutation is not yet justified. Novel shapes are
already emitted as privacy-safe fixture candidates for review.

## Product outcome

A Longhouse transcript should be readable as a document rather than a protocol
dump:

- prose remains primary;
- repetitive exploration collapses into concise, expandable groups;
- consequential actions, failures, approvals, and ambiguity remain prominent;
- provider wrappers recede only after their meaningful children are recovered;
- exact provider input, output, transport, and raw source are one disclosure
  action away; and
- web and iOS show the same semantic timeline.

At the system level, a provider format change should automatically produce a
specific diagnosis such as:

> Codex 1.42 introduced a new `custom_tool_call` envelope. Archive conservation
> still passes, but 18.4% of visible tool rows are now Unknown and the median
> rows per turn regressed from 4 to 17. Control is unaffected. Candidate fixture
> and adapter patch are attached.

## Why this is one system

Control and transcript rendering share the same provider boundary. A provider
adapter already needs to know how to start a session, address a turn, observe
events, and prove results. A second transcript-only provider registry would
duplicate identity, release, schema, evidence, and confidence logic.

The existing Universal Agent Harness and executable provider capability
contracts are the foundation. This epic extends them through the last mile:

```text
Provider contract
├── identity and release
├── execution and control
├── observation and raw capture
├── structural normalization
├── semantic presentation
└── executable proof and drift policy
```

## Existing foundation

Preserve and extend:

- `schemas/managed_providers.yml` as the authored provider contract;
- `server/zerg/qa/universal_agent_harness.py` as the universal scenario runner;
- provider adapter classes for Claude, Codex, OpenCode, Antigravity, and Cursor
  ingestion where applicable;
- executable capability assertions and trusted proof records;
- immutable evidence packages, accepted baselines, and old/new release diffing;
- raw-to-canonical ingest and SQLite projection tests;
- `longhouse translation evaluate` and its conservation accounting;
- `config/tool-tiers.json` as the shared presentation rule source; and
- generated web/iOS presentation mappings.

Do not restore the retired Python device runtime. The native Machine Agent owns
provider observation and control on the machine. The Python harness remains the
proof runner and artifact evaluator.

## Goals

1. Detect provider control, wire-format, ingest, and presentation regressions
   before users report them.
2. Make every provider event account for itself from raw archive through UI row.
3. Continuously inventory new tools, envelopes, argument shapes, results, and
   control behaviors from authorized traffic.
4. Turn novel evidence into redacted fixture candidates and reproducible failing
   cases.
5. Automatically handle safe, exact mappings and mechanically equivalent
   schema changes.
6. Generate reviewed remediation candidates for parsed or semantic changes.
7. Prove concise transcript behavior on representative historical sessions,
   not only hand-authored unit fixtures.
8. Keep web and iOS presentation behavior identical from one generated contract.
9. Make provider onboarding an adapter-and-proof exercise rather than a new
   product integration.
10. Expose clear factory health: archive correctness, structural correctness,
    semantic quality, visual quality, control capability, and drift.

## Non-goals

- A universal provider transport.
- A universal ontology of agent intent.
- LLM-generated summaries stored as transcript truth.
- Executing transcript contents during discovery or evaluation.
- Hiding raw provider evidence from power users.
- Treating high mapping coverage as proof of semantic correctness.
- Keeping old and new production behavior behind flags or compatibility paths.
- Requiring provider-specific UI renderers.
- Making every rare MCP tool look custom-designed.

## Governing principles

### Preserve signal; derive meaning late

The archive stores provider-shaped evidence. Canonicalization recovers only
structure that can be proven. Presentation is a versioned read-time lens.

```text
archive truth -> canonical logical events -> disposable presentation
```

The factory stores fingerprints, counts, relationships, outcomes, and evidence
references. It does not replace raw evidence with classifications.

### One contract, provider-specific mechanics

Shared scenarios and oracles define success. Provider adapters own mechanics.
The universal runner must not accumulate `if provider == ...` behavior.

### Unknown is a first-class successful outcome

An unknown but preserved event is safer than a confidently wrong label. Unknown
events render with their raw identity, remain searchable, and feed discovery.

### Consequence determines automation authority

Automation can be aggressive for exact, read-only equivalences and conservative
for mutations, failures, approvals, external effects, attribution, wrapper
recession, and inferred children.

### The corpus, not the user, finds regressions

Every rule and adapter change replays against adversarial golden fixtures in
hermetic CI. Authorized scheduled runs add historical sessions and a fresh
archive window; release runs add staged provider canaries. User reports are
additional evidence, never the primary monitoring system.

The factory also measures its own signal quality: shape churn, Yellow verdict
precision, duplicate candidates, fixture growth, and discovery freshness. If
these show that an automation layer creates more noise than value, that layer
does not advance.

### One current implementation

When a contract or rule is promoted, it replaces the prior implementation.
Longhouse does not serve two renderers, compatibility modes, or feature-flagged
translations. Previous rules and proof artifacts remain in git/artifact history
for diagnosis and rollback, not as live branches.

## Unified provider contract

Extend each provider declaration with references to five independently
versioned facets:

| Facet | Owns |
| --- | --- |
| Identity | Provider, release, binary, platform, adapter digest, wire families |
| Control | Launch, send, steer, interrupt, pause, resume, stop, runtime facts |
| Observation | Source locations, event families, cursors, ordering, raw capture |
| Normalization | Call/result pairing, envelope decoding, child extraction, provenance |
| Presentation | Exact aliases, parsed rules, salience, grouping, disclosure |

The authored contract remains compact. Large fingerprints, samples, and proof
records are generated artifacts, not hand-maintained YAML.

Illustrative declaration:

```yaml
provider: codex
adapter_revision: 12
adapter_sources:
  - engine/src/codex_bridge.rs
  - engine/src/codex_exec.rs
wire_families:
  - id: codex_jsonl
    discovery: native_archive
  - id: codex_app_server
    discovery: managed_bridge
normalization:
  ruleset: codex
  accepted_schema_fingerprints: generated
presentation:
  ruleset: shared_tool_presentation
proof_profiles:
  pull_request: hermetic
  release_candidate: staged_release
  continuous: privacy_safe_live_replay
```

## Evidence model

Every observed operation must retain a provenance chain:

```text
RawEvidenceRef
  source/provider/device/session/branch/offset/hash
        ↓
CanonicalEvent
  stable id/kind/call-result links/extraction status/rule version
        ↓
PresentationNode
  verb/summary/salience/group/disclosure/presentation version
        ↓
RenderedRow
  surface/version/fixture or sampled screenshot reference
```

### Raw evidence

- Immutable, provider-shaped, exportable, and tenant-scoped.
- Includes unknown fields and unrecognized event types.
- Content hashes allow replay identity without copying payloads into metrics.
- Never mutated by a normalization or presentation migration.

### Canonical event

- Deterministic and idempotent for a provider adapter/ruleset version.
- Carries source references and any enclosing wrapper.
- Marks origin as `stored`, `exact_alias`, or `parsed_child`.
- Does not infer intent, concurrency, or result attribution without wire evidence.
- Can be rebuilt from raw evidence.

### Presentation node

- Disposable and rebuildable.
- Uses the closed observable vocabulary from the tool translation experience.
- Carries salience and grouping without becoming archive authority.
- Exposes the exact canonical and raw evidence behind every summary.

## Automated discovery loop

Discovery begins schema-on-read over raw evidence Longhouse already owns. A
scheduled evaluator scans authorized archive manifests and staged provider
release evidence; it does not add a second ingest or telemetry pipeline.

The fingerprint format is stable from Phase 0 so a future high-volume runtime
counter can emit the same observations. That counter is built only if measured
volume or detection latency proves batch replay insufficient.

For each provider/wire family, inventory:

- event/envelope type;
- tool or method name;
- argument and result shape fingerprint;
- field presence and scalar/container types;
- call/result identity and pairing strategy;
- ordering, branch, and pagination behavior;
- wrapper/child relationships;
- outcome, failure, approval, and mutation signals;
- provider release, platform, adapter, and Longhouse build;
- canonical result and presentation disposition; and
- whether the shape has existing fixture and proof coverage.

Fingerprints describe structure, not values. Dynamic keys, IDs, paths, tokens,
timestamps, and payload strings are normalized before hashing.

### Discovery outputs

Every evaluation window produces four reports:

1. **Shape and Unknown report** — frequency, novelty, churn, consequence,
   fixture coverage, and ranked affected sessions.
2. **Conservation report** — pairing, orphans, duplicates, ambiguous fallback,
   attribution, and archive reachability.
3. **Presentation report** — rows per turn, grouping, salience, disclosure,
   determinism, bounds, and surface parity.
4. **Control report** — capability assertions by provider/release/context.

Novelty, coverage, and release comparisons are views over these reports, not
separate stored authorities.

## Corpus factory

The factory maintains three complementary corpora.

### Golden corpus

Small, source-controlled, privacy-safe fixtures with hand-verified expected
archive, canonical, and presentation results. Includes positive, adversarial,
malformed, interrupted, failure, approval, mutation, media, pagination, branch,
and rare-tool cases.

### Historical replay corpus

A local/authorized manifest over real archived sessions. Payloads remain in
their original authority boundary. The manifest records hashes, selections, and
safe metadata so runs are reproducible without committing transcripts.

Selection is stratified by provider, release, wire family, tool shape,
consequence, session size, failure state, mode, and age. It deliberately samples
outside the examples used to author a rule.

### Fresh archive window

A rolling authorized manifest selects recently ingested sessions for scheduled
replay. It catches new releases and workflows without sending new telemetry or
copying private payloads. Novel/high-impact shapes feed assisted fixture
creation.

### Assisted fixture creation

When a scenario fails, the factory extracts and redacts the smallest obvious
event window that reproduces the failure while preserving:

- provider/wire/release identity;
- call/result and wrapper relationships;
- field types and required sentinel values;
- ordering and branch boundaries; and
- the failing invariant.

No transcript string is executed. A reviewer authors the durable golden fixture
from this candidate, and the factory proves that it still reproduces the
invariant. Persisted fixtures pass secret scanning and contain only
synthetic/redacted values.

Candidates are fingerprint-deduplicated. Obsolete fixtures may be retired only
when no supported provider release, accepted baseline, or adversarial invariant
depends on them. Fixture count, runtime, and duplication are reported as a
budget. Full delta-debugging is reconsidered only when measured fixture volume
makes manual minimization a recurring cost.

## Normalization and presentation rule lifecycle

Every observed shape has one disposition:

| Disposition | Meaning |
| --- | --- |
| Exact | Provider evidence directly names the observable operation |
| Parsed | Deterministic structural rule recovers an operation with provenance |
| Generic | Known external/MCP operation rendered safely without a custom verb |
| Unknown | Preserved but not structurally understood |
| Invalid | Malformed or contract-violating evidence shown as such |

### Candidate generation

Novel shapes are clustered by structural similarity. Deterministic heuristics
may propose aliases, pairing rules, and wrapper extraction. An LLM may summarize
a cluster for a reviewer from authorized/redacted structural evidence. It does
not author golden fixtures, rules, adapter patches, or verdicts.

Every candidate includes:

- before/after canonical and presentation examples;
- affected provider releases and corpus counts;
- consequence classes;
- new positive and adversarial fixtures;
- conservation and attribution results;
- historical disagreement samples selected independently;
- expected viewport/row-count impact; and
- exact rollback commit/ruleset identity.

Promotion proof must include independently selected examples that were not used
to generate the candidate. A candidate's own fixtures can never be its sole
evidence.

### Promotion authority

The factory may automatically promote only Exact alias additions. A change is
mechanically exact only when it:

- maps one explicit provider-native tool name one-to-one onto an existing verb
  in the closed presentation vocabulary;
- introduces no structural rule and changes no call/result link;
- introduces no wrapper recession, child extraction, salience, or grouping
  change;
- affects only read-only events with no approval, failure, mutation, or
  external-effect ambiguity;
- passes conservation, attribution, determinism, disclosure, and surface parity;
- passes independently selected historical and fresh-window examples with no
  semantic disagreement; and
- passes the staged provider-release proof profile.

All Parsed, Generic, result-attribution, new-verb, salience, grouping, child,
and wrapper changes require review of the generated candidate. The factory still
performs discovery, evidence extraction, replay, and diagnosis automatically;
the reviewer decides only the semantic boundary.

Candidates are fingerprint-deduplicated and refreshed when new evidence
arrives. Stale candidates archive after 30 days without blocking a newer
candidate for the same signature. Candidate queue age and reviewer acceptance
rate are factory-health metrics, not product capability gates.

Promotion is an atomic source change to the single active ruleset. There is no
runtime flag and no dual behavior.

## Universal scenarios

Extend the Universal Agent Harness action matrix with a presentation-proof
family. Existing actions remain the control and ingest authority.

```text
raw_evidence_capture
provider_shape_inventory
schema_drift_detect
parse_normalize
call_result_conservation
db_ingest
session_projection
timeline_projection
presentation_projection
presentation_grouping
raw_disclosure_reachability
cross_surface_parity
historical_replay
live_window_replay
old_new_release_diff
```

The CLI exposes composite proof profiles rather than requiring callers to
orchestrate these individual actions:

- `capture_and_conserve`
- `normalize_and_project`
- `present_and_disclose`
- `control_surface`
- `drift_compare`

Individual actions remain in the evidence package for diagnosis and capability
accounting.

Each provider emits a result for every action. Unsupported, blocked, unobserved,
and unknown are explicit results rather than missing rows.

### Core invariants

1. Every raw event has exactly one accounting disposition.
2. No event, call, result, media item, or failure is silently lost or duplicated.
3. No result is attributed to more than one call.
4. Parsed children retain stable identity and wrapper/source provenance.
5. Ambiguous ordering, pairing, branches, rewinds, and pagination stay visible.
6. Unknown and invalid events always render.
7. Consequential actions, approvals, failures, partial parses, and orphans never
   recede into a low-salience group.
8. A collapsed group count and outcome equal its expandable children.
9. Every presentation claim is reachable to canonical and raw evidence in one
   disclosure action.
10. Projection is deterministic, idempotent, bounded, and safe for untrusted
    transcript input.
11. Provider-specific rules cannot fire for another provider/wire family.
12. Web and iOS produce equivalent presentation trees from the same contract.
13. Replaying a newer ruleset cannot mutate raw history or canonical evidence
    produced under an older adapter identity.
14. Control capability proof and presentation proof remain independent: one
    cannot mask failure in the other.
15. Any reduction in raw-evidence reachability or disclosure is a Red
    presentation verdict regardless of other improvements.
16. Identical canonical events and rulesets produce byte-equivalent
    presentation trees across locale, timezone, machine, web, and iOS.
17. Presentation generation is bounded in time, memory, depth, children per
    group, and visible rows. Overflow degrades to an explicit generic/raw group,
    never truncation disguised as completeness.
18. Adapter identity covers every declared adapter source, including native
    Rust observation/control sources; an incomplete digest cannot qualify proof.
19. A promoted candidate must pass independently selected evidence outside the
    fixtures that proposed it.
20. Stale or failed discovery makes factory health `unknown`, halts automatic
    Exact promotion, and never pretends the last run is current.

## Quality scorecard

Never combine the following into one vanity percentage.

### Archive correctness

- raw capture completeness;
- export/read success;
- source hash stability;
- truncation/media accounting; and
- authorization/provenance completeness.

Target: zero unexplained loss or duplication.

### Structural correctness

- pairing and attribution precision;
- orphan/duplicate/ambiguous counts;
- deterministic replay;
- fixture coverage by observed shape; and
- exact/parsed/generic/unknown disposition.

Target: zero false attribution; all ambiguity explicit.

### Semantic quality

- independently sampled label precision by consequence class;
- old/new disagreement rate;
- parsed-rule precision;
- generic/unknown rate by visible rows and affected sessions; and
- raw-evidence audit success.

Target: no semantic regression in consequential classes; Unknown is preferable
to an unproven label.

### Visual quality

- visible tool rows per turn and per assistant paragraph;
- viewport share consumed by supporting tool activity;
- consecutive low-salience row runs;
- expansion/disclosure correctness;
- failure/approval/mutation salience; and
- web/iOS snapshot parity.

Targets are corpus distributions, not a fixed global row limit. The factory
flags statistically meaningful regressions against the accepted baseline and
keeps representative worst-case screenshots.

### Control quality

- implemented capability assertions with fresh applicable proof;
- provider release/version coverage;
- launch/send/steer/interrupt/resume success;
- runtime state truthfulness; and
- orphaned-process and transcript-binding failures.

### Factory quality

- observed shape changes per provider release and month;
- time from first observed drift to reproducible evidence;
- Yellow verdict precision and repeated-noise rate;
- candidate deduplication, age, and acceptance;
- fixture count, duplication, and proof runtime; and
- discovery freshness and last complete window.

These metrics decide whether heavier automation is justified. A noisy Yellow
digest or a candidate system that mostly produces rejected work does not expand
automatically.

## Drift detection and verdicts

Drift is evaluated by provider, release, wire family, platform, adapter digest,
and Longhouse projection version.

Each run publishes two verdicts plus an aggregate summary that never weakens
either one.

### Control verdict

| Verdict | Meaning | Automated response |
| --- | --- | --- |
| Green | Required control assertions hold for the evaluated context | Publish trusted capability proof |
| Yellow | Missing/stale proof, unsupported gap, or incomplete diagnosable evidence | Preserve declared disposition; warn or deny according to capability contract |
| Red | Broken control path, false capability claim, unsafe cleanup, or identity failure | Fail control proof and deny the affected action |

### Transcript verdict

| Verdict | Meaning | Automated response |
| --- | --- | --- |
| Green | Archive, structural, semantic, disclosure, and parity invariants hold | Record proof and continue |
| Yellow | New preserved Unknown, missing baseline, or bounded quality regression | Rank evidence and create an assisted candidate |
| Red | Loss, duplication, false attribution, hidden consequence/failure, broken disclosure, or severe concision regression | Fail transcript proof and serve explicit generic/raw-visible projection |

The response never drops the provider archive. When presentation proof fails,
the safe behavior is the generic/raw-visible projection. When control proof
fails, capability evaluation denies or warns according to the executable
capability contract. These are normal contract outcomes, not feature flags.

If a scheduled discovery window does not complete within twice its expected
interval, factory health becomes `unknown` with reason `discovery_stale` and
automatic Exact promotion halts. The last successful verdict remains historical
evidence, not current health.

### Detection triggers

- every Longhouse pull request that changes adapters, ingest, projection, rules,
  or clients;
- every supported provider release candidate and installed version change;
- scheduled replay of the rolling live window;
- statistically significant shape or Unknown-rate drift;
- a new high-consequence or high-volume shape;
- archive/pairing invariant failure;
- web/iOS contract or screenshot divergence; and
- manual invocation for forensic comparison.

Pull requests use the source-controlled golden corpus and hermetic canaries.
Historical and fresh-window replay run locally or on an authorized scheduled
worker because private transcripts do not enter public CI. Provider release
proof uses staged synthetic/no-token/live-token profiles as appropriate.

## Commands and artifacts

Extend existing commands rather than creating a parallel service or broad new
CLI family:

```bash
longhouse translation evaluate --corpus <manifest> --profile <profile> --json
scripts/qa/universal-agent-harness.py --profile <profile> --json
scripts/qa/provider-release-proof.py --run-universal-harness ...
longhouse provider factory status [--provider codex] [--json]
```

`factory status` is a read-only aggregate over the two existing proof surfaces.
Unknown ranking, old/new diff, and candidate patches are evidence-package views,
not separate command authorities. Accepting a candidate is a normal atomic
source change followed by generation and proof; it never toggles runtime
behavior.

Each run writes an immutable evidence package:

```text
run.json                       # manifest + contract + adapter identity
action-matrix.json
shape-inventory.json
conservation-report.json
control-report.json
presentation-report.json
baseline-diff.json
candidate-patches/
fixture-candidates/
screenshots/
verdict.json
```

Unknown signatures and surface parity are sections of the shape and
presentation reports. The package extends the Universal Agent Harness evidence
schema; it does not define a second artifact protocol.

Raw payloads are referenced in their authorized store and are not copied into
portable CI artifacts.

## Runtime and UI observability

The default product UI does not show factory dashboards. It shows trustworthy
transcripts. Power-user and maintainer diagnostics expose:

- presentation disposition and rule version;
- provider/wire/adapter identity;
- enclosing wrapper and extracted-child provenance;
- why an event remained Generic or Unknown;
- raw evidence and export;
- transcript completeness warnings; and
- an evidence link to the relevant factory verdict.

A small maintainer health surface may show provider verdict, last proof, drift,
Unknown visible-row rate, and failing invariant. The canonical output remains
the command/artifact contract so Sauron and future automation can consume it.

## Privacy and security

- Structural observations are tenant-scoped and value-free by default.
- Shape normalization removes dynamic keys and sensitive scalar values before
  hashing or aggregation.
- Historical replay uses authorized manifests and runs locally by default.
- CI receives synthetic/redacted fixtures, never private transcripts.
- Candidate examples pass secret scanning before persistence.
- Transcript content is data, never executable parser source.
- LLM-assisted cluster explanation is optional, uses redacted evidence, and
  follows the configured credential/data authority. It does not author proof.
- Artifact retention is explicit; raw references expire independently from
  non-sensitive structural metrics.

## Delivery plan

### Phase 0 — Parsed-rule entry proof and contract consolidation

- Complete `tool-translation-evaluation.md` Phase 2 on one Parsed Codex wrapper
  rule using independently sampled historical sessions.
- Record semantic precision, review effort, visible-row impact, and observed
  shape churn rather than merely recording parser match rate.
- Extend the managed-provider contract with wire, normalization, presentation,
  and proof-profile references.
- Add Cursor ingestion to the same observation/normalization vocabulary without
  pretending it has the same managed-control capabilities.
- Freeze the evidence provenance chain and generated artifact schemas.
- Merge translation evaluation actions into the Universal Agent Harness action
  matrix and verify adapter digests cover native Rust sources.
- Delete duplicated provider-name and tool-alias authorities.

Gate: the Parsed rule passes its existing safety gate and demonstrates material
reading improvement; a fresh agent can locate one provider declaration, one
adapter identity, one action matrix, and one proof package per provider. If the
Parsed rule does not earn its complexity, the factory remains Exact/Generic and
still proceeds with conservation and drift proof.

### Phase 1 — Batch structural discovery and unified proof

- Scan the existing historical archive, fresh authorized window, and staged
  release evidence using privacy-safe structural fingerprints.
- Build the four discovery reports and establish provider/release/wire baselines.
- Extract redacted fixture candidates for novel or failing shapes.
- Implement composite harness profiles and the new invariants.
- Make adapter, ingest, projection, rule, web, and iOS changes select the
  correct hermetic proof profile automatically.
- Publish independent control and transcript verdicts in one evidence package.

Gate: a synthetic schema change, broken steer, lost result, false attribution,
hidden failure, disclosure regression, and cross-client mismatch are each
detected, ranked, and attributed without a user report. Private historical
replay succeeds on the authorized scheduled worker; public CI remains hermetic.

### Phase 2 — Transcript concision and direct renderer cutover

- Make semantic presentation trees the only web/iOS tool-rendering input.
- Implement grouping and wrapper recession only where review gates pass.
- Add tree-level structural diffs and distribution-based viewport/row-density
  regression checks.
- Run the old and new presentation projectors offline against the same golden
  and historical corpora. This is proof comparison, not a runtime feature flag.
- Require zero disagreement for failures, approvals, mutations, external
  effects, attribution, and raw disclosure.
- Capture representative and worst-case web/iOS fixtures.
- Execute and verify one rollback drill that restores the prior presentation
  tree identity from source history.

Gate: representative transcripts are concise, expand losslessly, keep
consequential events prominent, and match across web/iOS. The new projector
replaces the old renderer atomically and the old renderer is deleted.

### Phase 3 — Candidate assistance and bounded automation

Entry requires measured recurring shape churn or Unknown impact that justifies
more than reports and assisted fixture extraction.

- Cluster, deduplicate, and rank novel shapes.
- Generate Exact-alias candidates and reviewer-ready Parsed/semantic evidence
  packages with before/after corpus impact.
- Automatically promote only the mechanically exact aliases defined above.
- Require explicit acceptance for all Parsed, Generic, structural, semantic,
  salience, grouping, and recession changes.
- Make accepted candidates atomically replace the active ruleset and regenerate
  both clients.
- Track Yellow precision, candidate acceptance, queue age, and fixture budget;
  stop expanding automation when these measures are noisy.

Gate: a synthetic exact rename travels from observation through independent
proof to atomic promotion; a wrapper change becomes review-ready but cannot
auto-promote; unsafe recession is mechanically rejected.

### Downstream operations boundary

Provider release watching, schedules, private credentials, notifications, and
adoption policy remain Sauron responsibilities. Sauron consumes the factory's
control and transcript verdicts and invokes its release profile; it does not
define their semantics.

Every future provider or materially new wire family must onboard through the
unified contract, adapter, evidence chain, and universal scenarios. This is a
standing conformance requirement, not a later factory phase.

Continuous Machine Agent structural counters and automatic delta-debugging are
future optimizations. They require evidence from factory-quality metrics that
batch replay or assisted fixture extraction is insufficient.

## Cutover policy

This is a direct replacement, not a compatibility migration.

- Each phase removes the authority it supersedes in the same change.
- No feature flags, legacy render modes, or dual provider registries.
- Generated artifacts may retain schema versions for reproducibility, but only
  one current projection is served.
- Rollback means reverting the atomic source change and rebuilding projections;
  it does not mean maintaining both paths indefinitely.
- Historical raw evidence remains readable regardless of current presentation
  version.

## Failure drills

Before completion, the factory must catch injected examples of:

- renamed provider tool and envelope types;
- a call ID reused across branches;
- a result arriving before or without its call;
- a wrapper containing multiple operations and a truncated child;
- an edit mislabeled as a read;
- a nonzero shell result collapsed into a successful exploration group;
- an approval request receded behind tool noise;
- a provider release breaking steer but not send;
- a malformed event designed to stress parser time or memory;
- a new MCP tool with sensitive-looking fields;
- server/web/iOS disagreement on grouping; and
- a session with dozens of consecutive reads, greps, waits, and nested Codex
  execution wrappers.
- stale discovery incorrectly reported as current;
- an adapter digest that omits a native Rust source;
- a candidate that passes only its own generated examples;
- loss of raw disclosure while the summary remains readable;
- presentation-tree overflow that must degrade explicitly; and
- an executed renderer rollback that must restore the prior tree identity.

## Risks and controls

| Risk | Control |
| --- | --- |
| High coverage but wrong semantics | Independent samples and consequence-sliced precision |
| Automation hides evidence | Raw archive immutable; Unknown and failures fail open visually |
| Registry becomes a policy language | Compact authored references; generated observations stay artifacts |
| LLM proposals become authority | Deterministic oracles decide; models only explain redacted clusters |
| Private corpus leaks | Local manifests, structural fingerprints, redaction, secret scanning |
| CI becomes slow or expensive | Hermetic PR profile; authorized scheduled historical/fresh/release profiles |
| Provider branches infect shared code | Adapter conformance and standing new-provider factory requirement |
| Visual metrics reward over-collapsing | Salience invariants and consequence-specific fixtures |
| Factory blocks ingest on novelty | Unknown remains preserved and renderable; only false claims fail |
| Two systems survive cutover | Phase gate requires deleting superseded authority |
| Yellow noise makes alerts useless | Track Yellow precision, deduplicate signatures, and stop noisy expansion |
| Factory silently stops observing | Discovery freshness is an explicit health gate |

## Definition of done

The epic is complete when:

1. One provider contract explains control, observation, normalization,
   presentation, and proof for every supported provider.
2. Every archived event is conserved and traceable through the rendered UI.
3. New provider shapes and capability regressions are detected from releases or
   real traffic without a user report.
4. The factory produces a redacted fixture candidate, diagnosis, and remediation
   evidence package for supported drift classes.
5. Safe exact changes can promote automatically; semantic or consequential
   changes arrive review-ready.
6. Web and iOS consume one generated presentation contract and pass parity proof.
7. Representative transcripts are concise while failures, approvals, edits,
   external effects, and uncertainty remain prominent.
8. Raw evidence is reachable in one action and export remains lossless.
9. A staged provider release receives independent trusted control and transcript
   verdicts before adoption.
10. No old renderer, compatibility flag, duplicate provider registry, or
    provider-specific client logic remains.

## Success statement

Longhouse should learn that a provider changed before the user notices. It
should preserve the unfamiliar evidence, explain the blast radius, construct a
reproducible test, propose the smallest safe adaptation, and prove the resulting
control path and transcript across historical data, current traffic, web, and
iOS.

The factory is successful when provider change becomes routine evidence and a
bounded repair—not a month of dogfooding and scattered bug reports.
