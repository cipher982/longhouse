# Tool Translation Evaluation and Delivery

Status: council-refined plan; discuss before starting
Owner: Longhouse timeline
Depends on: `tool-translation-experience.md`

## Goal

Ship a cross-provider semantic reading layer without using weeks of manual
dogfooding to discover basic mapping errors. Use deterministic replay for what
can be proven mechanically and focused human review for semantic and visual
judgment.

The working frame is narrower than “build a translation platform”:

- archive and structural translation behave like a compiler;
- semantic labels require independently reviewed examples;
- visual hierarchy requires fixture-backed UI review and dogfooding.

Do not build registry services, coverage APIs, or an automated mapping factory
until one Parsed provider rule proves that recurring complexity exists.

## Empirical baseline

A read-only 90-day local archive spike found:

| Provider | Calls sampled | Dominant raw tools |
|---|---:|---|
| Claude | 62,975 | Bash 55%, Read 18%, Edit 12% |
| Codex | 325,900 outer | exec_command 54%, exec 18%, stdin 13%, patch 7% |
| Cursor | 24,384 | Read 34%, Grep 26%, Shell 23% |
| OpenCode | 18,129 | bash 41%, read 30%, grep 12% |
| Antigravity | 4,088 | view 40%, grep 24%, command 14% |

The common shape is small: shell, read, search/list, edit/write, wait, MCP,
and agent/subagent cover most calls. Long tails are mainly rare MCP or vendor
tools: 16–91 distinct names per provider.

Codex contained 57,836 stored `exec` wrappers. A spike recovered nested method
names from 57,724 (99.81%) by parsing wrapper JavaScript, but those are inferred
from string input, not stored provider leaf events. Claude and Codex raw
call/result IDs were nearly complete, with five orphan calls each.

This corpus is one user's workflow, one OS, English-heavy, and Codex-dominated.
Claude/Codex raw distributions are high-confidence; Cursor/OpenCode/Antigravity
second-pass parsers are medium-confidence; hosted Codex event mix is unreliable.
Provider gates must not pretend these evidence levels are equivalent.

## Four scorecards

### 1. Archive correctness

Are provider records complete, immutable, retrievable, authorized, and
exportable? Translation work cannot compensate for archive loss.

### 2. Structural correctness

Are calls/results conserved, uniquely paired, provenance-linked, ordered only
as evidence permits, and stable across replay?

### 3. Semantic quality

Does a label such as `Read`, `Searched`, or `Viewed` accurately describe the
observable operation? Rule match rate is not accuracy. Independently sampled
rendered examples are the oracle.

### 4. Visual quality

Does the timeline make prose, failures, uncertainty, consequential actions,
and raw evidence appropriately salient? Deterministic replay cannot answer
this; UI fixtures and dogfooding must.

Never combine these into one “percentage pretty” score.

## Evaluation command

Provide one read-only entry point:

```bash
longhouse translation evaluate --corpus <manifest> --json
```

It emits a local JSON artifact, not a served API. For each provider/wire family
and rule version, report:

- source events, outer calls, results, and logical-operation accounting;
- stored, extracted, duplicated, lost, orphaned, and unattributed records;
- explicit aliases, inferred children, Unknowns, and blocked recession;
- metrics weighted by event, logical operation, session, and visible row;
- consequence slices: read-only, mutation, failure, approval, external effect;
- rule matches, schema-shape drift, and old/new disagreement examples.

Inferred children remain a separate accounting class. They never inflate Exact
coverage or get added to outer-call totals as though both were independent work.

## Correctness invariants

Automated replay must prove:

1. Every source event lands in exactly one accounting outcome.
2. Calls/results are neither silently dropped nor duplicated.
3. No result attaches to more than one call.
4. Extracted children link to their wrapper, source, session, branch, timestamp,
   media, truncation state, and active-context boundary.
5. Duplicate/reused IDs, FIFO fallback, rewinds, pagination boundaries, and
   provisional events fail visibly when ambiguous.
6. Rule matching is exclusive or follows deterministic precedence.
7. Failed, orphaned, pending, unattributed, or partially parsed calls do not
   recede.
8. Unknown inputs render with raw identity; every canonical event renders to
   something.
9. Collapsed summary counts equal their expandable children.
10. Translation is deterministic, idempotent, bounded against pathological
    input, and treats transcript content as untrusted data.
11. Provider-specific rules cannot fire across providers.
12. Server, web, and iOS agree on pairing and projection fixtures.

Golden fixtures include positive, adversarial, malformed, interrupted,
failure, approval, mutation, media, large-output, branch, and rare-tool cases.
Changed examples must be selected independently of the rule author's fixtures.

## Rules and Unknown report

Use a versioned source-controlled rules file with fixtures, extending the
existing `tool-tiers.json` and shell-salience pattern. Do not introduce a
registry service or datastore.

The evaluator emits a ranked Unknown-signature report with counts, provider,
argument/result shape, and safe evidence references. Unknown is an acceptable
steady state for rare tools; do not chase the tail for vanity coverage.

Automation may inventory, cluster, select examples, and draft fixture/rule
proposals. Human review is required for any rule that:

- changes the user-visible verb;
- reduces salience or joins a collapsed group;
- recedes a wrapper;
- synthesizes children from string parsing;
- affects mutation, approval, failure, external effects, or attribution.

No autonomous rule promotion is in the initial epic.

## Privacy and provenance

Historical transcripts may contain credentials, paths, customer data, prompts,
commands, and tool output. The evaluation corpus must be a reproducible manifest
over authorized local evidence, read-only and local by default.

- Metrics and CI artifacts contain no raw payloads or secrets.
- Evidence samples are redacted before persistence or model review.
- Coverage artifacts remain tenant-scoped and access-controlled.
- Transcript content is never executed or treated as trusted parser code.
- Sending samples to a model requires the same explicit data/credential
  authority as any other external disclosure.
- Provider/wire provenance is recorded when available; otherwise rules key on a
  versioned schema-shape fingerprint and monitor distribution drift.

## Minimal delivery plan

### Phase 0 — Evidence contract

- Fix string-valued tool input in machine event/detail contracts.
- Prove raw inspection/export for real data from all supported providers.
- Define provider/wire provenance and schema fingerprints.
- Freeze a privacy-safe corpus manifest plus a small independently validated
  golden subset.

Gate: real machine endpoints validate across providers; replay is demonstrably
read-only; every golden event traces to raw evidence.

Stop if raw evidence is lossy or provider identity cannot be established. That
becomes an archive epic, not a presentation workaround.

### Phase 1 — Accounting evaluator plus Exact aliases

- Implement replay accounting and the invariants above.
- Add only explicit native tool aliases through the shared rules/config path.
- Render Exact aliases behind a dogfood flag to exercise the whole disclosure
  path early.
- Review a stratified set of rendered sessions per provider.

Gate: zero loss, duplication, or false attribution in the golden corpus;
stable identities; server/web/iOS parity; Unknown shapes retain raw rendering.

Stop if Exact-only presentation already solves the reading problem for a
provider. Parsed machinery is not mandatory.

### Phase 2 — One Parsed Codex rule

Use Codex `exec` wrapper extraction as the single test of whether Parsed
translation earns its complexity.

- Keep inferred children visibly marked and separately counted.
- Initially show extracted contents without receding the wrapper.
- Run historical replay plus a live-window replay.
- Review random rendered examples, not only successful fixtures.
- Measure semantic precision separately for safe reads, mutations, failures,
  and unattributed results.

Gate: zero loss/duplication/false attribution/hidden failures; acceptable
independently reviewed semantic precision for each affected consequence class;
bounded parser behavior; raw evidence reachable in one action.

Stop if string inference cannot establish trustworthy attribution or its review
burden exceeds the UX value. Fall back to `Contains N inferred operations`
without promoting children or receding the wrapper.

### Phase 3 — Presentation dogfood and scope decision

- Consider wrapper recession only after Phase 2 evidence passes.
- Test semantic and visual quality separately on web and iOS.
- Include at least one workflow not represented by the corpus author before
  any default-on decision.
- Pin projection/rule versions so rollback restores stable identities and
  rendering, not merely labels.

Gate: consequential and failed operations retain salience; uncertainty is
visible; raw evidence is reachable in one action; users prefer the translated
view on representative sessions.

Epic stop condition: if remaining Unknowns are rare tools that read acceptably
with generic raw/MCP rendering, stop. Build broader drift services, mapping
factories, or a coverage endpoint only if observed rule volume and churn justify
them.

## Success

The first cut succeeds when Longhouse can reproducibly show:

- whether the archive and current projections conserve provider evidence;
- which exact aliases improve reading without changing salience;
- which calls remain Unknown and why;
- how a proposed rule changes representative historical sessions;
- whether semantic labels pass independent review;
- whether every displayed claim remains auditable to raw evidence.

The smallest valuable deliverable is Phase 0 plus Phase 1. Starting the full
automation factory is explicitly not authorized by this plan.
