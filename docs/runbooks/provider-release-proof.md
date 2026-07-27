# Provider Release Proof Runbook

Use this runbook to generate, compare, accept, and inspect provider proof
artifacts. The design contract is `docs/specs/provider-release-proof.md`; the
next build-identity work is `docs/specs/provider-build-matrix.md`.

## Rules

- Pass provider binaries explicitly. The harness does not search ambient PATH.
- Use generated fakes for hermetic CI and exact staged artifacts for upstream
  release proof.
- Keep live-token profiles opt-in. They may spend provider tokens.
- Treat execution status, assertion outcome, operational policy, and human
  assessment as separate facts.
- Accept only green, complete, identity-bound proof artifacts.
- Never promote evidence across provider version, executable identity,
  Longhouse SHA, scenario revision, oracle digest, platform, or architecture.

## Hermetic All-Provider Smoke

```bash
make provider-release-proof-universal-smoke \
  ARTIFACT=/tmp/provider-release-proof-universal-smoke.json \
  EVIDENCE_ROOT=/tmp/provider-release-proof-universal-smoke-evidence
```

This generates disposable fake binaries and runs the universal harness across
the managed-provider contract. Yellow is valid when the artifact contains only
typed unsupported operations or stronger-evidence gaps. Red means the harness,
projection, or executable scenario regressed.

The smoke writes:

- `universal-agent-harness.json`;
- provider support and execution coverage matrices;
- raw scenario evidence; and
- `provider-release-proof-maturity.json`.

## One Explicit Provider Binary

```bash
make provider-release-proof \
  PROVIDER=opencode \
  PROVIDER_BIN=/absolute/path/to/opencode \
  ARTIFACT=/tmp/opencode-proof.json \
  EVIDENCE_ROOT=/tmp/opencode-proof-evidence \
  SOURCE_REVIEW_STATUS=pass \
  SOURCE_REVIEW_NOTE="Reviewed the upstream release and found no declared contract change."
```

The provider path must identify the artifact intended for qualification. Do not
pass `$(command -v ...)` unless the installed artifact is deliberately the
subject of a manual diagnostic run.

To attach selected universal scenarios:

```bash
scripts/qa/provider-release-proof.py \
  --provider opencode \
  --provider-bin /absolute/path/to/opencode \
  --artifact /tmp/opencode-proof.json \
  --evidence-root /tmp/opencode-proof-evidence \
  --run-universal-harness \
  --universal-scenario probe_identity \
  --universal-scenario full_action_suite \
  --json
```

## Universal Harness Directly

One provider:

```bash
scripts/qa/universal-agent-harness.py \
  --provider codex \
  --provider-bin /absolute/path/to/codex \
  --scenario probe_identity \
  --evidence-root /tmp/longhouse-universal-harness \
  --json
```

Multiple providers require a provider-qualified path for every input:

```bash
scripts/qa/universal-agent-harness.py \
  --provider claude \
  --provider codex \
  --provider opencode \
  --provider-bin claude=/absolute/path/to/claude \
  --provider-bin codex=/absolute/path/to/codex \
  --provider-bin opencode=/absolute/path/to/opencode \
  --scenario action_matrix \
  --scenario control_surface \
  --evidence-root /tmp/longhouse-universal-actions \
  --json
```

Scenarios that do not execute a provider may still receive the explicit build
mapping. Keeping it present prevents a future scenario change from silently
switching to an installed laptop binary.

## Real Installed-Provider Smoke

The universal smoke has one opt-in acquisition boundary that may resolve named
operator overrides and PATH:

```bash
scripts/qa/provider-release-proof-universal-smoke.py \
  --use-real-provider-bins \
  --scenario probe_identity \
  --artifact /tmp/installed-provider-smoke.json \
  --evidence-root /tmp/installed-provider-smoke-evidence \
  --json
```

The entry point resolves each provider once, passes the resulting map into the
fail-closed harness, and records `provider_bin_sources`. Missing providers stay
missing; the harness does not retry against PATH.

Add `--include-live-token-streaming` only when token spend is intended.

## Old/New Differential

Stage both artifacts outside the harness, then pass exact paths and expected
versions:

```bash
make provider-release-proof-staged-old-new \
  PROVIDER=opencode \
  OLD_PROVIDER_BIN=/tmp/staged/opencode-old \
  NEW_PROVIDER_BIN=/tmp/staged/opencode-new \
  OLD_PROVIDER_VERSION="opencode 1.2.3" \
  NEW_PROVIDER_VERSION="opencode 1.2.4" \
  OLD_PROVIDER_SOURCE_URI="release://opencode/1.2.3" \
  NEW_PROVIDER_SOURCE_URI="release://opencode/1.2.4" \
  ARTIFACT=/tmp/staged-old-new-proof.json
```

Inspect the two proof artifacts before the normalized diff. A shared
infrastructure failure is not a compatibility pass. A difference is actionable
only when the compared identities and scenario inputs are compatible.

## Baselines

The local default baseline root is `.provider-release-proofs`. Maintainer
dogfood may use `~/.local/share/longhouse/provider-release-proofs`. Private
factory storage is operator-owned and is not part of this public runbook.

Accept a green artifact:

```bash
make provider-release-proof-accept \
  PROOF=/tmp/opencode-proof.json \
  BASELINE_ROOT=.provider-release-proofs
```

Compare a new artifact:

```bash
make provider-release-proof-diff \
  CANDIDATE=/tmp/opencode-proof.json \
  BASELINE_ROOT=.provider-release-proofs
```

Check maturity and accepted coverage:

```bash
make provider-release-proof-maturity \
  BASELINE_ROOT=.provider-release-proofs \
  ARTIFACT=/tmp/provider-release-proof-maturity.json
```

Accepted artifacts are immutable evidence. Reaccept a scenario only after
reviewing the new raw proof and understanding every changed assertion.

## Private Release Factory Boundary

The private factory may:

- discover releases and maintain cursors;
- acquire exact npm or release assets;
- verify upstream integrity metadata;
- supply scoped credentials to live profiles;
- invoke these public commands against a pinned clean Longhouse checkout;
- retain artifacts and publish trusted proof records; and
- schedule notifications and token spend.

It must not duplicate provider support maps, scenario lists, assertion
semantics, or baseline pass criteria. A new provider is incomplete until its
private acquisition lane agrees with the public managed-provider contract, or
is explicitly declared manual/observed-only.

## Reading Results

Start with:

1. `execution_status` — did the runner complete?
2. provider and Longhouse identities — was the intended subject tested?
3. coverage completeness — are all required assertions present?
4. assertion outcomes — what passed, failed, or remained blocked?
5. raw references — what evidence supports the result?
6. old/new diff — what changed after identities and inputs were matched?

Common setup failures:

- `provider_binary_not_found` — no explicit provider build was supplied;
- `provider_binary_version_mismatch` — staged artifact and requested version
  disagree;
- `provider_release_proof_prerequisites_missing` — a required runtime or
  credential input is absent;
- `baseline_missing` — no accepted artifact exists for this scenario;
- `insufficient_coverage` — the run completed without every required assertion;
- `provider_release_proof_drift` — comparable proof artifacts disagree.

## Updating Coverage

When proof strength changes:

1. update `docs/specs/provider-release-proof-coverage.json`;
2. update the two generated snapshot tables in
   `docs/specs/provider-release-proof.md`;
3. keep accepted scenario identities explicit; and
4. run the provider validation target.

## Validation

```bash
make validate-provider-cli-canaries
make test
```

Use the smallest target during iteration. The exact-SHA CI and ship lanes own
broad final validation.
