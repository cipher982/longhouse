# Executable Provider Capability Contract Epic

**Status:** Approved for phased implementation after external review
**Owner:** Longhouse
**Updated:** 2026-07-22
**Scope:** Managed-provider contracts, executable proof, contextual evaluation,
provider parity, and product-facing capability truth

## Executive Decision

Longhouse will evolve its existing provider registry into an executable
capability contract. The contract separates four questions that static support
booleans currently conflate:

1. **Disposition:** does upstream afford the semantic, and has Longhouse
   implemented and enabled it?
2. **Verification:** which required behavioral assertions have applicable,
   trusted proof?
3. **Runtime:** does this machine and session satisfy the operation's control,
   mode, phase, permission, and health prerequisites?
4. **Product action:** should this consumer hide, disable, warn, or enable the
   operation in this evaluation context?

```text
declaration + applicable proof assertions + evaluation context
    -> disposition + verification + runtime + action + reasons
```

The evaluator is a pure projection, not a new service. Shared code owns
semantics, proof requirements, evaluation, and reporting. Provider-specific
bridges, hooks, plugins, and launchers continue to own execution.

## Context and Motivation

Longhouse already has a strong base:

- `schemas/managed_providers.yml` is the authored registry;
- generated artifacts and validators keep declarations complete;
- `provider_support_state.py` joins contracts, proof, CLI, and control facts;
- release proof, local proof, canaries, and the universal harness supply useful
  evidence classes; and
- local health exposes readiness and proof maturity.

That system cannot yet answer the real product question:

> Can this Longhouse implementation perform this operation through this
> provider in this machine/session context, and what evidence justifies the
> claim and product behavior?

Today static `supported_operations` gates real actions even when proof is
absent or inapplicable. Manifest target levels can present as observed maturity.
Proof is not consistently bound to the provider adapter and scenario it
exercised. One mutable proof file per provider amplifies staleness across
independent operations. Cursor has valuable deep tests but is not uniformly in
the common proof pipeline.

Coordination exposed this weakness. One `startup_coordination_context` boolean
cannot express awareness on create, awareness after compaction, message
delivery, or terminal silence. Nor can it say whether a limitation is upstream,
unimplemented, disabled, unproven, or unavailable for this session.

## Current-State Assessment

### Good and worth preserving

- One authored registry and generated package artifact.
- Explicit provider mechanics rather than a pretend-generic transport.
- Complete declarations for the current operation set.
- Separate release, local-proof, and runtime feeds.
- Existing provider/version/freshness checks.
- Reusable scenario/artifact foundations in the universal harness.
- Real canaries for steer, recovery, hook claims, rollout binding, and Cursor
  Helm behavior.

### Bad, ambiguous, or lacking

- Static booleans are product truth.
- Upstream, implementation, policy, proof, and runtime are conflated.
- Evidence declarations can self-certify.
- Applicability differs across proof consumers.
- Proof is not scoped to relevant provider/adapter/oracle inputs.
- A provider-level artifact creates false-negative amplification.
- One flaky run can dominate a prior valid pass.
- Provider-wide readiness can leak into session-specific action enablement.
- Unsupported reasons are prose rather than executable facts.
- Fake/no-token mechanics checks can be mistaken for semantic proof.
- Cursor is a proof island.

### Audit and review outcome

A source audit rated the current system **3.5/5: evidence-aware capability
registry**: strong schema integrity, weaker separation, identity, provider
parity, and negative semantic proof.

The first draft was independently reviewed by Hatch Claude Fable, Codex Sol,
and OpenRouter Kimi K3. All approved the core direction with required structural
changes. This revision adopts their shared conclusions:

- proof sufficiency is predicate-based, not a scalar rank;
- identity is scoped to provider implementation and scenario, not global SHA;
- disposition, verification, runtime, and action remain independent;
- evaluation always receives an explicit machine/session context;
- proofs are append-only per-scenario records;
- a failed run never erases an unexpired applicable pass;
- the evaluator runs in shadow mode before gating actions;
- coordination is an early vertical slice and parallel parity track; and
- authoring remains flat and compact instead of becoming a policy DSL.

## Goals

1. Make every product-visible provider action mechanically explainable.
2. Separate disposition, resolved policy, verification, and runtime readiness.
3. Prove claims with named assertions and trusted artifacts.
4. Preserve raw passes, failures, and inapplicable evidence.
5. Share semantic oracles while keeping provider drivers independent.
6. Bind proof only to inputs relevant to the tested semantic.
7. Make unsupported behavior structured and executable.
8. Bring Cursor into the common proof pipeline honestly.
9. Fail new-provider onboarding closed.
10. Deliver coordination parity wherever upstream permits it.
11. Migrate without destabilizing current launch/control paths.

## Non-Goals

- One generic provider transport.
- Automatic enablement after an upstream feature appears.
- Live-token tests on every pull request.
- A general policy language, evidence DSL, or new availability service.
- Treating source review as behavioral proof.
- Applying high-risk proof requirements to every observability fact.
- Letting transient provider outages erase implementation truth.
- Rewriting the universal harness before reusing it.

## Design Principles

### Shared semantics, split execution

Scenarios define provider-independent assertions. Provider drivers perform setup
and emit observations. A common oracle determines outcomes; drivers cannot
redefine success.

### Evidence never self-certifies

Declarations identify required assertions and acceptable evidence classes.
Only executed scenarios from trusted producers qualify. Legacy
manifest-derived evidence remains inspectable but cannot promote verification.

### Evidence class is not a total ordering

`hermetic`, `live_no_token`, and `live_token` describe production context.
A live help probe may prove less than a hermetic behavioral assertion. Each
required assertion names acceptable evidence classes and contexts.

### Unknown is not false; proof is not runtime

Missing proof does not rewrite implementation as unsupported. A disconnected
session does not erase a valid proof. Each axis changes independently.

### Claims and execution safety use explicit policy

Verification governs what Longhouse may call proven. Execution gating is
authored by action risk:

- **ceiling:** read/discovery may use implemented disposition;
- **warn:** recoverable mutations may run with a visible unverified warning;
- **strict:** high-risk mutations require fresh qualifying proof.

Dispatch re-evaluates the same contract at send time. A UI snapshot cannot
bypass enforcement, and proof-lane flakiness cannot silently brick warn-tier
behavior.

## Capability Declaration

### Stable semantic IDs

IDs describe semantics; mode and transport are constraints. The initial session
set maps all current operations, including one-shot work:

```text
session.launch
session.turn.start
session.run_once
session.resume
session.reattach
session.input.send_idle
session.input.steer_active
session.interrupt.active
session.pause.answer
session.terminate
session.transcript.tail
session.transcript.bind
session.runtime.phase
```

The initial coordination set is intentionally consumer-backed:

```text
coordination.awareness.create
coordination.awareness.post_compaction
coordination.message.send
coordination.message.receive
```

Resume is an awareness test context. Reply, acknowledgement, and terminal noise
remain scenario assertions until a distinct consumer requires capability IDs.
Missing entries evaluate unverified; aspirational parity is not pre-authored.

### Compact authored form

```yaml
capabilities:
  session.input.steer_active:
    disposition: upstream_absent
    reason_code: upstream_unavailable
    mechanism: null
    contexts:
      modes: [helm]
    policy_key: provider.cursor.active_steer
    action_gate: strict
    required_assertions:
      - id: active_turn_nonce_observed_before_turn_end
        acceptable_evidence: [live_token]
      - id: no_second_user_turn_created
        acceptable_evidence: [hermetic, live_token]
    runtime_prerequisites: []
```

Disposition is `implemented | not_implemented | upstream_absent |
policy_disabled`. Optional upstream/policy overrides appear only when needed.
Policy declarations name a key/default; `EvaluationContext` supplies the
resolved tenant, edition, rollout, and permission value with provenance.

`experimental` is a rollout/policy state, not a disposition. It still
requires implemented semantics and normal proof. New-provider validation fails
when an implemented capability lacks required assertion/scenario references.

### Reason codes

```text
upstream_unavailable
upstream_unknown
longhouse_unimplemented
policy_disabled
semantic_proof_missing
semantic_proof_failed
semantic_proof_stale
evidence_class_insufficient
proof_provider_version_mismatch
proof_manifest_mismatch
proof_adapter_mismatch
proof_scenario_revision_mismatch
proof_oracle_mismatch
proof_platform_mismatch
proof_untrusted_producer
cli_unavailable
runtime_unavailable
runtime_unhealthy
runtime_not_advertised
mode_unsupported
phase_unsupported
permission_mode_unsupported
```

Details preserve paths, versions, and provider-specific explanations.

## Proof Contract

### Append-only assertion records

The durable unit is one record per provider scenario run. A run may emit
multiple assertion records, but never overwrites history or a previous pass. A
provider index accelerates lookup without becoming authority.

Each record contains:

```text
artifact schema/kind and content-derived artifact ID
provider, provider version, and resolved executable identity
provider-contract entry digest and provider adapter bundle digest
scenario ID/revision, oracle digest, assertion ID, and outcome
mode, permission mode, platform, and architecture
evidence class and generated_at
producer class/version, invocation ID, and run reference
raw evidence reference digests
Longhouse build ID and Git SHA for forensics
```

Outcomes are `pass | semantic_fail | infrastructure_error | blocked |
skipped`. Only `pass` qualifies. Other outcomes remain visible without
pretending infrastructure failure disproved provider semantics.

### Scoped identity

Applicability gates on provider-entry, adapter-bundle, scenario/oracle, provider
executable, and relevant context identity. Whole-repo Git SHA and distribution
build ID are forensic only. Editing Claude must not invalidate Codex proof.

Canonical provider-entry digests come from the generator. Adapter bundle
digests cover declared provider adapter source roots. Scenario/oracle digests
come from their code modules.

Version matching defaults exact. A provider may opt into a reviewed
compatibility rule with a reason. Version changes trigger cheap local
hermetic/stock re-proof; scheduled live lanes backstop model-visible behavior.
Resolve the real executable when possible because npm/shell shim digests can
hide payload changes.

### Applicability and sufficiency

A record qualifies only when:

1. schema, kind, provider, assertion, and scenario are recognized;
2. its producer is trusted for the consuming policy;
3. executable identity/version policy matches;
4. provider entry, adapter, scenario, and oracle identities match;
5. mode, permission, platform, and architecture apply;
6. outcome is `pass`;
7. consumer-side freshness has not expired; and
8. its evidence class is acceptable for that assertion.

Verification is proven only when every required assertion predicate has an
applicable pass. There is no `observed_level >= minimum_level` shortcut.
Freshness defaults are consumer-owned per evidence class with a clock-skew
backstop.

### Failure accumulation

Evaluation selects the newest applicable pass inside its freshness window, not
simply the newest run. A later failure:

- never erases an earlier valid pass;
- sets `latest_run_failed` with its own outcome/reason;
- feeds alerts and confidence presentation; and
- moves verification only when no qualifying pass survives.

Local/manual artifacts remain useful diagnostics. Release claims accept only
designated CI or authenticated local-machine publishers. Signing is deferred.

## Evaluation Contract

### EvaluationContext

Every decision includes:

```text
machine and optional session identity
provider process and resolved executable identity
Shadow, Helm, or Console mode
permission mode and per-action grant
control path, connection, and lease identity
provider/runtime phase
resolved policy values and provenance
observed_at
```

Provider-wide status may describe installation readiness but cannot enable a
session action. Session actions require session-scoped facts.

### Orthogonal results and action precedence

```text
disposition: implemented | not_implemented | upstream_absent | policy_disabled
verification: proven | missing | stale | failed | inconclusive | inapplicable
runtime: ready | not_required | unavailable | unhealthy | unknown
action: enabled | enabled_with_warning | disabled | hidden
```

| Condition | Action |
| --- | --- |
| Not applicable to this mode/consumer | `hidden` |
| Upstream absent, unimplemented, or policy disabled | `disabled` |
| Required runtime not ready | `disabled` |
| Strict gate and verification not proven | `disabled` |
| Warn gate and verification not proven | `enabled_with_warning` |
| Ceiling gate with implemented disposition | `enabled` |
| All required facts qualify | `enabled` |

Results include reason codes, applicable/inapplicable records, latest-run
signals, runtime observations, and an input-bundle digest for reconstruction.

### API transition

- `implemented_operations`: implementation ceiling;
- `operation_decisions`: full contextual results; and
- `available_operations`: compatibility projection of enabled operations.

`supported_operations` is a time-boxed deprecated alias. A drift test
eventually prevents the server from emitting it; unknowable external usage does
not preserve it forever.

## Executable Conformance

Scenarios/oracles live in code. The registry references immutable scenario,
assertion, and oracle IDs plus parameters/applicability. Provider drivers emit
observations and raw artifacts; the common oracle determines outcomes.

Representative assertions:

- active steer: nonce observed before the same turn ends; no fabricated turn;
- native resume: same provider conversation and Longhouse binding;
- coordination: peer discovery and nonce delivery after create, resume, and
  compaction without duplicate visible bootstrap events;
- negative steer: typed rejection before PTY/control write and no advertised
  action.

Executable negative conformance is required for product-reachable/high-risk
unsupported actions. One generic boundary rejection per provider covers other
unreachable declarations. Source review documents upstream absence; it cannot
prove Longhouse rejects without side effects.

Observability-only facts may remain plain implementation/runtime facts rather
than inherit high-risk proof requirements for symmetry.

## CI and Proof Lanes

### Pull request

- schema/generation/onboarding validation;
- evaluator truth and precedence tables;
- applicability/trust tests;
- hermetic assertion/oracle tests;
- high-risk negative conformance;
- artifact atomicity/index tests; and
- coverage validation that every required PR scenario ran.

### Stock binary

- scheduled and version-triggered;
- resolves the real executable where possible;
- proves hooks, plugins, handshakes, binding, cleanup, create/attach/resume, and
  other non-token mechanics;
- emits per-scenario `live_no_token` records; and
- retains enough pinned package identity to reproduce recent runs where
  licensing permits.

### Live semantic

- scheduled and risk/release gated;
- proves model-visible behavior with bounded sessions/nonces;
- emits per-scenario `live_token` records;
- separates semantic failure from infrastructure/provider outage; and
- reruns unchanged versions to detect server-side drift.

Lane coverage is machine-readable. Expected unsupported counts only when the
negative boundary assertion passes.

## Provider Workstreams

### Claude and Codex vertical slice

The first end-to-end slice implements coordination awareness and messaging for
Claude and Codex through declarations, proof records, the shadow evaluator, and
one real consumer. Claude maps channel/hook/`claude_print`; Codex maps
app-server/MCP/bridge/`codex_exec`. Both prove create, resume, and compaction
awareness without terminal bootstrap cards.

### Cursor convergence

Cursor becomes a first-class proof producer/consumer while preserving Helm Gate
0, product E2E, storage-v2 honesty, and `cursor_print`. Active-turn steer
remains upstream-absent until a real upstream semantic passes conformance.

### OpenCode and Antigravity

OpenCode reuses launch-scoped plugin and serve/attach paths while keeping idle
send, interrupt, terminate, and steer distinct. Antigravity reuses the hook
inbox/PreInvocation surface and runs proof-first spikes before claiming
coordination, reattach, Console, interrupt, or steer.

Parity means equal honesty and common semantics, not identical mechanisms or
supported sets.

## Delivery Plan

### Implementation status — 2026-07-22

- Phases 0–2 are implemented: stable semantics, append-only assertion records,
  scoped applicability, exact-artifact trust inputs, contextual evaluation,
  and the `operation_decisions` diagnostic projection.
- Phase 3 is active: CI emits an immutable hermetic proof bundle with raw
  evidence, stock OpenCode executes its launch-scoped configuration, and
  Cursor/Antigravity reject unsupported active steer before control writes.
  The current CI bundle intentionally uses a fixture provider identity to test
  the pipeline; it cannot qualify a real installed provider. Authenticated CI
  artifact fetch and stock/live proof backfill remain Phase 3 work.
- Phase 4 has one deliberately narrow consumer: OpenCode Helm coordination
  injection consults the shared evaluator under a ceiling policy. Mutation
  dispatch remains on the legacy gates until authenticated release proof is
  fetched and re-evaluated with session-scoped runtime facts.
- Cursor and Antigravity do not declare coordination awareness. Their current
  upstreams have no launch-scoped instruction override that Longhouse can use
  without mutating user workspace/global configuration.

### Phase 0 — Freeze semantics and product behavior

Deliver semantic mappings, all state enums, action precedence, gate policy,
experimental semantics, reason codes, and an inventory of static-support
consumers. Accept when every current operation including `run_once` maps
without reinterpretation and the state-to-affordance table is tested.

### Phase 1 — Proof records and assertion predicates

Deliver append-only artifact v2, assertion outcomes, producer trust, scoped
identity derivation, applicability, and non-qualifying legacy import. Accept
when declarations cannot self-certify, unrelated provider edits do not
invalidate proof, failures preserve valid passes, and qualifying assertions
link to trusted records.

### Phase 2 — Shadow evaluator plus Claude/Codex slice

Deliver explicit `EvaluationContext`, `operation_decisions`, input-bundle
digests, supported-versus-decided diff reports, and Claude/Codex coordination
scenarios through one non-gating consumer. Accept when provider-wide facts
cannot enable session actions, decisions reconstruct exactly, and diffs are
reviewed before cutover.

### Phase 3 — Conformance and requalification

Deliver common code-owned oracles, provider drivers, reachable positive and
negative scenarios, local re-proof on version change, scheduled drift
detection, coverage manifests, and proof backfill for selected capabilities.
No action switches without applicable v2 proof.

### Phase 4 — Incremental product cutover

Convert one capability family and surface at a time, beginning with the proven
Claude/Codex slice. UI and dispatch use the same evaluator. Remove each static
gate only after shadow comparison and proof backfill. Remove the legacy alias
after its deprecation window and server drift test.

### Parallel A — Cursor proof convergence

Add Cursor to universal harness, release/local proof, and evaluator consumption
without blocking cutover of already-proven operations.

### Parallel B — Remaining coordination parity

Implement awareness and messaging for Cursor, OpenCode, and Antigravity where
upstream permits. Prove create/resume/compaction behavior and terminal silence;
report exact limitations where parity is impossible.

## Observability and Operator UX

Diagnostics answer separately: implemented? proven? runtime-ready here? which
policy produced the action?

```text
cursor session.input.steer_active
  disposition: upstream_absent
  verification: proven negative boundary rejection
  runtime: not_required
  action: disabled (upstream_unavailable)

codex coordination.awareness.post_compaction
  disposition: implemented
  verification: proven (live_token assertion set)
  runtime: ready for session 123 / Helm
  action: enabled
```

Local health links qualifying and rejected records and shows
`latest_run_failed` separately from the capability decision.

## Risks and Controls

| Risk | Control |
| --- | --- |
| Schema becomes a language | Flat declarations; code-owned scenarios/oracles |
| Every commit invalidates proof | Provider/adapter/scenario digests; SHA forensic only |
| Provider auto-update removes actions | Compatibility policy, local re-proof, tiered gates |
| Flaky run disables working feature | Append-only records; valid pass survives |
| Provider artifact amplifies staleness | Per-scenario records and assertion applicability |
| Provider state enables session action | Mandatory session `EvaluationContext` |
| Hand-authored result qualifies | Trusted producers and designated roots |
| New upstream feature auto-enables | Source review cannot grant proof |
| Hard provider blocks all cutover | Cursor/coordination as parallel tracks |
| UI and dispatch disagree | Same evaluator, rerun at dispatch |
| Duplicate truth lives forever | Deprecation window, inventory, drift tests |

## Success and Definition of Done

- Every in-repo action decision comes from the contextual evaluator.
- Every proven claim links its complete assertion set to trusted raw evidence.
- Every high-risk unsupported action has typed no-side-effect rejection proof.
- Drift produces explainable verification, never false support or global
  invalidation.
- A failed run does not erase an independent valid pass.
- All five providers participate in common records/evaluation or declare an
  exact limitation.
- Coordination create/resume/compaction behavior is proven wherever upstream
  permits it, without terminal duplication.
- Every decision reconstructs from declaration, policy, proof, runtime, and
  context inputs.

For every operation Longhouse must mechanically answer:

```text
What is its upstream/implementation disposition?
Which resolved policy applies?
Which assertions prove it, and why do those records apply?
What does the latest failed run say without erasing valid proof?
Is this exact machine/session ready now?
Why is the action hidden, disabled, warned, or enabled?
```

Display and dispatch derive those answers from one generated
declaration/proof/runtime projection, not provider-specific UI conditionals.
