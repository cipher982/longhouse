# Provider Release Proof

**Status:** active evidence contract; build identity hardening is proposed
**Owner:** Longhouse
**Last updated:** 2026-07-27

## Decision

Longhouse defines what provider compatibility means. A private release factory
decides when to acquire releases, spend tokens, retain evidence, and publish
trusted proof records. Operational scheduling never becomes a second source of
provider capability truth.

The public boundary consists of:

- `schemas/managed_providers.yml`, the authored provider and operation contract;
- `server/zerg/qa/universal_agent_harness.py`, the shared scenario runner;
- provider qualification profiles and deterministic oracles under
  `server/zerg/qa/`;
- `docs/specs/provider-release-proof-coverage.json`, the auditable coverage
  snapshot; and
- append-only proof artifacts whose identities bind provider, Longhouse,
  scenario, oracle, input, platform, and architecture.

The private release lane owns acquisition, credentials, schedules, retention,
notifications, and publication policy. It invokes the public runner through an
artifact and CLI boundary. It does not redefine scenarios, assertions, support
flags, or pass criteria.

Operator commands live in `docs/runbooks/provider-release-proof.md`. The next
build-identity design is `docs/specs/provider-build-matrix.md`.

## Required Proof Loop

```text
declared provider release
  -> exact acquired artifact
  -> explicit provider build input
  -> versioned Longhouse scenario + oracle
  -> immutable raw evidence
  -> scoped assertion records
  -> optional old/new comparison
  -> trusted publication
```

Execution health, assertion outcome, operational policy, and human assessment
remain separate facts. A runner failure is not provider incompatibility. A
model explanation cannot create a passing assertion. A baseline comparison
cannot turn missing evidence into proof.

## Provider Scope

The managed-provider contract is the provider authority.

- Claude, Codex, OpenCode, and Cursor are launch tier.
- Antigravity is maintenance tier. Existing ingest, archive, transcript, and
  release evidence keep working; new control investment is excluded unless the
  product decision changes.
- Cursor release history is forward-only when upstream builds can only be
  observed. Captured builds use `observed_only` provenance and are never
  presented as requestable historical releases.

`outstanding_factory_work()` is the implementation backlog. Settled upstream
absence and policy-disabled routes are typed facts rather than Yellow gaps.

## Execution Lanes

### Hermetic pull-request lane

CI uses generated fake providers, fixtures, isolated SQLite databases, and
deterministic oracles. Every binary path is injected explicitly. An omitted
provider build fails closed; the harness never searches the runner's ambient
`PATH`.

This lane proves Longhouse behavior. It does not prove that the current
upstream provider still behaves the same way.

### Staged release lane

The release factory acquires an exact upstream artifact, verifies its published
identity, and passes its explicit path and expected version into the public
qualification runner. Provider credentials are supplied only to profiles that
require them and are never serialized into requests or retained evidence.

The current identity profile hashes the selected executable. The planned build
matrix widens this to the full execution closure and verifies that closure
before and after execution.

### Manual live lane

Token-spending or machine-specific canaries may run manually against an
explicitly selected installed provider. Their evidence must identify the exact
provider artifact and cannot be promoted to another version or machine by
inference.

## Fail-Closed Rules

- A harness scenario receives an explicit provider path or reports
  `provider_binary_not_found`.
- Operator/debug environment overrides are named inputs. Omission never means
  “whatever this machine has installed.”
- PATH lookup belongs only at an explicit acquisition entry point, and its
  result is passed into the harness and recorded.
- A release-factory request binds a clean Longhouse SHA, provider version,
  provider identity, invocation id, profile, and run reference.
- Provider and Longhouse identities are verified again after execution.
- Missing, partial, malformed, or coverage-incomplete runs remain operational
  evidence and do not advance release cursors or publish qualifying records.
- Raw provider evidence remains authoritative; projections and comparisons are
  disposable derived views.

## Coverage Snapshot

The machine-checkable source is
`docs/specs/provider-release-proof-coverage.json`. The following tables are
validated against it by `scripts/tests/provider-release-proof-coverage.test.py`.

| Metric | Count |
| --- | ---: |
| Providers | 5 |
| Contract surfaces per provider | 13 |
| Total provider/surface rows | 65 |
| Covered `yes` | 19 |
| Covered `partial` | 43 |
| Covered `no` | 3 |
| Rows running in Longhouse CI | 51 |
| Rows running in the private release lane | 34 |
| Rows with accepted parser-fixture baselines | 3 |
| Rows with accepted release-proof baselines | 41 |

| Provider | Yes | Partial | No | CI rows | Private lane rows | Release baselines |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Claude Code | 2 | 11 | 0 | 12 | 7 | 3 |
| Codex/OpenAI | 4 | 9 | 0 | 12 | 10 | 9 |
| OpenCode | 4 | 9 | 0 | 13 | 12 | 11 |
| Antigravity | 1 | 9 | 3 | 9 | 5 | 5 |
| Cursor | 8 | 5 | 0 | 5 | 0 | 13 |

These counts describe recorded coverage, not universal release safety. A
`partial` row may prove Longhouse plumbing without proving current upstream
behavior. An accepted baseline is scoped to its scenario and identity; it does
not cover adjacent actions.

## Evidence Maturity

Operation evidence uses:

- `none` — no proof;
- `source_review` — documented mechanics only;
- `hermetic` — deterministic Longhouse-side proof;
- `live_no_token` — real provider behavior without model spend; and
- `live_token` — provider/model-visible behavior.

Implementation disposition is separate:

- `implemented` participates normally in proof;
- `not_implemented` is Longhouse backlog and names an owner action;
- `upstream_absent` is a version-scoped settled fact; and
- `policy_disabled` names the route Longhouse deliberately uses instead.

## Baselines and Differentials

Accepted baselines are immutable, scoped artifacts. Old/new comparison is
useful only when both sides carry compatible provider, Longhouse, scenario,
oracle, input, platform, architecture, and coupling identities.

A provider release does not invalidate unrelated Longhouse components. Future
invalidation and skip logic must derive from declared dependencies. Skip logic
is deferred because an incorrect skip removes the detection signal itself.

## Current Gap

Provider builds are still represented too narrowly in parts of the proof path.
An entrypoint digest misses npm payload trees, sibling assets, symlink targets,
interpreters, plugins, and self-updated files. The build-matrix work therefore
proceeds in this order:

1. keep the harness fail closed and make acquisition explicit;
2. store generated fakes with closure manifests;
3. publish provider channel declarations and stage real releases through the
   existing private factory; and
4. add sparse scheduling or replay optimization only after identity stability
   is measured.

No new scheduler, credential broker, dashboard, or backward Cursor archive is
part of this work.
