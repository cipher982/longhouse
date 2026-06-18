# Provider Release Proof

**Status:** Phase 1 inventory + initial Longhouse entrypoint
**Owner:** David
**Last updated:** 2026-06-18

## Purpose

Longhouse needs to know whether a new upstream provider CLI release still
satisfies the contracts Longhouse depends on. Release notes and source review
are not enough. The desired proof loop is:

```text
known-good provider version -> Longhouse proof artifact A
new provider version        -> Longhouse proof artifact B
normalize both
diff required contract fields
```

Sauron should watch releases, stage binaries, call the Longhouse proof lane,
archive artifacts, compare baselines, and alert. Longhouse owns the provider
contract scenarios and the meaning of pass/fail.

## Current Answer

Longhouse has broad CI and several provider canaries, but CI is not yet an
all-encapsulating upstream-provider release gate.

What exists:

- backend, engine, frontend, runner, and Playwright E2E suites
- managed-provider contract manifest:
  `server/zerg/config/managed_provider_contracts.json`
- provider canary validation lane:
  `make validate-provider-cli-canaries`
- parser goldens for Claude, Codex, and Gemini:
  `engine/tests/golden_parser_contract.rs`
- provider release/live/control canary scripts under `scripts/qa/`
- Sauron release-watch provider-status publication

What is missing:

- exact old-version vs new-version differential execution
- accepted release-proof baselines per provider/scenario
- raw-to-normalized proof fixtures for all release-sensitive surfaces
- a single Longhouse-owned proof artifact consumed by Sauron for every provider

## Coverage Legend

`yes` means the current suite directly exercises the surface. A `yes` row with
`Baseline: no` is still not a complete release gate; it means the operation can
be proved today, but old/new baseline comparison is not wired yet. `partial`
means some lower layer or fake boundary exists, but the proof is not enough to
trust a new upstream release. `no` means no meaningful current proof was found.

Boundary values:

- `fixture` - committed parser or JSON fixture
- `hermetic` - fake process/API/server; good for Longhouse logic, weak for upstream drift
- `live_no_token` - real provider binary behavior without model spend
- `live_token` - real provider/model-visible behavior
- `source` - agent/source review only

Baseline means an accepted normalized output exists for the release-proof
surface, not merely a unit-test expected value unless called out.

## Phase 1 Coverage Map

The machine-checkable map lives in
`docs/specs/provider-release-proof-coverage.json` and is validated by
`scripts/tests/provider-release-proof-coverage.test.py`, which runs under
`make validate-provider-cli-canaries`. The tables below are the readable
summary; update the JSON first when a provider/surface changes.

### Claude Code

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron stages npm-sourced `@anthropic-ai/claude-code@version` into an isolated artifact root and passes `.bin/claude` to profile/live canaries | isolated npm package | no Longhouse CI; Sauron tests cover it | yes for npm releases | no | yes if staging/version match fails |
| binary identity | yes | `provider-live-canary --provider claude`, `provider-release-profile-canary.py` | live_no_token or fake | `validate-provider-cli-canaries` | yes, through provider status | no | yes if binary missing/version fails |
| auth/status shape | partial | `provider-live-canary --provider claude` binary/auth/channel checks | live_no_token | `validate-provider-cli-canaries` | yes if live proof configured | no | yes if red |
| launch managed session | partial | `provider-control-e2e-canary.py`, `test_claude_channel_launch_cli.py` | hermetic | `validate-provider-cli-canaries`, `make test` | profile/live gate only | no | partial |
| session id/path binding | partial | `test_claude_channel_bridge.py`, hook/session tests | hermetic | `make test` | no dedicated baseline | no | partial |
| transcript/log parse | yes | engine Claude golden + adversarial parser tests | fixture | `make test-engine` | source review only | parser fixture yes; release-proof no | yes for parser drift |
| ingest into Longhouse | partial | shipper E2E, Claude hook/outbox tests | fixture/hermetic | `make test`, `make test-shipper-e2e` | no dedicated release proof | no | partial |
| timeline/session projection | partial | session capability/messages/view tests | hermetic | `make test` | no | no | partial |
| send input | partial | managed-local chat/channel bridge tests | hermetic | `make test` | live proof only if configured | no | partial |
| interrupt/abort/steer | partial | Claude interrupt/steer channel tests; managed Claude POC is manual/live | hermetic + manual live_token | `make test`; manual target | no scheduled baseline | no | partial |
| reattach/resume | partial | channel bridge resume/state tests | hermetic | `make test` | no dedicated baseline | no | partial |
| tool/tool-result shape | partial | parser/tool-result tests cover transcript shapes | fixture/hermetic | `make test`, `make test-engine` | source review only | parser fixture yes; release-proof no | partial |
| live-token behavior | partial | `make managed-claude-poc` | live_token manual | no normal CI | no | no | no |

Claude risk: high. Closed source and release notes are not enough. Needs the
first real no-token release-proof scenario after OpenCode/Codex shape stabilizes.

### Codex

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron stages the exact Codex GitHub release asset and passes it to `codex-provider-release-canary.py`; Longhouse tests the binary override path | real release asset | no Longhouse CI for asset staging; canary override runs in CI | yes for source-reviewed GitHub releases | no | yes if staging/version match fails |
| binary identity | yes | `codex-provider-release-canary.py` | live_no_token or fake | `validate-provider-cli-canaries` | yes | no | yes |
| auth/status shape | partial | static contract + app-server canary lanes | hermetic/live_no_token when enabled | `validate-provider-cli-canaries` | yes | no | partial |
| launch managed session | yes | Codex bridge tests, `codex-provider-release-canary.py managed_tui_attach` | hermetic/live_no_token | `make test`, `validate-provider-cli-canaries` | yes | no | yes if canary red |
| session id/path binding | yes | `test_codex_bridge_contract.py`, engine state contract | hermetic | `make test` | provider status indirect | no | yes |
| transcript/log parse | yes | engine Codex golden + adversarial parser tests | fixture | `make test-engine` | source review only | parser fixture yes; release-proof no | yes for parser drift |
| ingest into Longhouse | partial | hook/outbox tests, shipper E2E | fixture/hermetic | `make test`, `make test-shipper-e2e` | no dedicated release proof | no | partial |
| timeline/session projection | partial | session capabilities/messages/views | hermetic | `make test` | no | no | partial |
| send input | partial | engine bridge IPC turn/start tests | hermetic | `make test`, engine tests | Sauron canary when configured | no | partial |
| interrupt/abort/steer | partial | engine bridge interrupt/steer tests | hermetic | `make test`, engine tests | Sauron canary when configured | no | partial |
| reattach/resume | partial | managed TUI attach canary; resume path tests | hermetic/live_no_token | `validate-provider-cli-canaries` | yes | no | partial |
| tool/tool-result shape | partial | Codex parser fixtures and tool-call tests | fixture/hermetic | `make test`, `make test-engine` | source review only | parser fixture yes; release-proof no | partial |
| live-token behavior | no | next notes in manifest call this out | none | no | no | no | no |

Codex is the strongest existing provider lane, but it still needs to be wrapped
into an accepted release-proof baseline and old/new differential runner.

### OpenCode

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron OpenCode release asset staging | real release asset | Sauron tests | yes | no | yes if staging fails |
| binary identity | yes | `provider-live-canary --provider opencode` | live_no_token or fake | `validate-provider-cli-canaries` | yes | no | yes |
| auth/status shape | partial | server health/auth/doc checks | live_no_token | `validate-provider-cli-canaries` | yes | no | yes |
| launch managed session | yes | provider live canary server/session checks; channel CLI tests | live_no_token + hermetic | `validate-provider-cli-canaries`, `make test` | yes | no | yes |
| session id/path binding | partial | OpenCode bridge/channel state tests | hermetic | `make test` | provider-live sidecar classification | no | partial |
| transcript/log parse | partial | live canary `session.messages` marker; no engine parser golden | live_no_token | `validate-provider-cli-canaries` | yes | release-proof no | partial |
| ingest into Longhouse | partial | provider-live session classification and route tests | hermetic/live_no_token | `make test`, `validate-provider-cli-canaries` | yes | no | partial |
| timeline/session projection | partial | session capability/view tests for OpenCode transport | hermetic | `make test` | no dedicated baseline | no | partial |
| send input | yes | provider-live canary `prompt_async` noReply marker | live_no_token | `validate-provider-cli-canaries` | yes | no | yes |
| interrupt/abort/steer | partial | provider-live abort endpoint; steer unsupported | live_no_token | `validate-provider-cli-canaries` | yes for interrupt | no | yes for abort, no for steer |
| reattach/resume | yes | provider-live process restart/session recovery + attach shape | live_no_token | `validate-provider-cli-canaries` | yes | no | yes |
| tool/tool-result shape | no | no OpenCode parser/tool result golden found | none | no | no | no | no |
| live-token behavior | no | manifest marks prompt/abort proof as next release lane | none | no | no | no | no |

OpenCode is the best first provider for the proof lane because it has release
asset staging and a no-token live server canary. Sauron now has a first
candidate-envelope wrapper, but Longhouse did not previously own a
`provider_release_proof` artifact.

### Antigravity

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | no | none | none | no | source/profile only | no | no |
| binary identity | yes | `provider-live-canary --provider antigravity`, profile canary | live_no_token or fake | `validate-provider-cli-canaries` | yes | no | yes |
| auth/status shape | partial | version/help/plugin/global hook checks | live_no_token | `validate-provider-cli-canaries` | yes | no | partial |
| launch managed session | partial | hook/plugin checks only | live_no_token/hermetic | `validate-provider-cli-canaries` | profile/live only | no | partial |
| session id/path binding | partial | hook binding tests | hermetic | `make test` | no | no | partial |
| transcript/log parse | partial | hook transcript binding tests | hermetic | `make test` | no | no | partial |
| ingest into Longhouse | partial | hook outbox/runtime tests | hermetic | `make test` | no | no | partial |
| timeline/session projection | partial | session capabilities for Antigravity transport | hermetic | `make test` | no | no | partial |
| send input | partial | `provider-control-e2e-canary.py --antigravity-real-agy-send` | live_token when explicitly run | wrapper test in CI uses fake agy | profile/live only | no | partial |
| interrupt/abort/steer | no | unsupported in manifest | none | no | no | no | no |
| reattach/resume | no | unsupported in manifest | none | no | no | no | no |
| tool/tool-result shape | no | no provider transcript parser golden found | none | no | no | no | no |
| live-token behavior | partial | real agy send canary exists but is not a scheduled CI/release lane | live_token manual/configured | fake-wrapper CI only | no | no | no |

Antigravity should stay narrow: prove the hook inbox actually changes the
model-visible turn, then keep unsupported operations explicit.

### Gemini CLI

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | no | none | none | no | source review only | no | no |
| binary identity | no | no managed Gemini contract in manifest | none | no | no provider status | no | no |
| auth/status shape | no | none | none | no | no | no | no |
| launch managed session | no | managed Gemini not launch-critical today | none | no | no | no | no |
| session id/path binding | no | none | none | no | no | no | no |
| transcript/log parse | yes | engine Gemini golden + adversarial parser tests | fixture | `make test-engine` | source review only | parser fixture yes; release-proof no | yes for parser drift |
| ingest into Longhouse | partial | parser/shipper generic path only | fixture | `make test-engine`, shipper tests | no | no | partial |
| timeline/session projection | partial | generic session projection tests | hermetic | `make test` | no | no | partial |
| send input | no | no managed Gemini control path | none | no | no | no | no |
| interrupt/abort/steer | no | no managed Gemini control path | none | no | no | no | no |
| reattach/resume | no | no managed Gemini control path | none | no | no | no | no |
| tool/tool-result shape | partial | Gemini parser adversarial fixtures | fixture | `make test-engine` | source review only | parser fixture yes; release-proof no | partial |
| live-token behavior | no | none | none | no | no | no | no |

Gemini should remain parser-first until managed Gemini control becomes a product
surface.

## Phase 2 Entry Point

The Longhouse-owned operator entrypoint is:

```bash
make provider-release-proof \
  PROVIDER=opencode \
  PROVIDER_BIN=/path/to/opencode \
  ARTIFACT=/tmp/proof.json \
  EVIDENCE_ROOT=/tmp/proof-evidence
```

Optional variables:

- `PROVIDER_VERSION` records an externally staged version when the source
  canary cannot infer it.
- `SOURCE_REVIEW_STATUS` and `SOURCE_REVIEW_NOTE` pass Codex/Sauron source
  review evidence through without fabricating it.
- `TIMEOUT_SECS` bounds the wrapped source canary.
- `CODEX_RUN_FAKE_APP_SERVER`, `CODEX_RUN_MANAGED_TUI_ATTACH`, and
  `CODEX_RUN_DETACHED_UI` enable opt-in Codex canary lanes.

The equivalent direct script entrypoint is:

```bash
scripts/qa/provider-release-proof.py \
  --provider opencode \
  --provider-bin /path/to/opencode \
  --artifact /tmp/proof.json \
  --evidence-root /tmp/proof-evidence \
  --json
```

It emits:

```json
{
  "schema_version": 1,
  "artifact_kind": "provider_release_proof",
  "provider": "opencode",
  "provider_version": "opencode 1.2.3",
  "scenario_id": "opencode-release-proof-v1",
  "verdict": "green",
  "failure_code": null,
  "operation_evidence": {},
  "normalized": {},
  "artifacts": {
    "source_artifact": "...",
    "stdout": "...",
    "stderr": "...",
    "normalized_contract": "..."
  }
}
```

Current implementation wraps existing source canaries:

- Claude/OpenCode/Antigravity: `scripts/qa/provider-live-canary.py`
- Codex: `scripts/qa/codex-provider-release-canary.py`
- Gemini: explicit yellow `provider_release_proof_not_implemented`

Claude npm release-watch ticks now have exact-version package staging in Sauron:
`@anthropic-ai/claude-code@<version>` is installed under the release artifact
root and the staged `.bin/claude` path is passed to Longhouse profile/live
canaries. This proves package staging and binary identity for npm-sourced
release events, but not a full managed-session baseline yet.

Claude normalization preserves no-token launch-contract shape: missing launch
flags from `claude --help`, development-channel status/missing flags, and
detached PTY wrapper status/platform. Failure codes and reasons stay in the
typed Claude block so a dev-channel contract break differs from local PTY
environment failure in old/new diffs. This is not yet a full managed-session
launch proof or exact-version staged package lane.

Codex normalization preserves source-review status, binary identity presence,
operation evidence, canary statuses/reasons, and stable protocol fingerprints
from `raw_fresh_remote` while dropping noisy path fields. A protocol fingerprint
status change such as `ok` -> `missing` is contract drift signal.

Exit-code contract:

- `red` exits `1`.
- `yellow` and `green` exit `0`.
- Automation callers must parse `verdict`; `yellow` is an honest proof gap, not
  a process failure.

For Codex, `--source-review-status` defaults to `not_run` and is passed through
instead of being fabricated by the wrapper. Sauron may pass `pass`, `warn`, or
`fail` only when it has actual source-review evidence.

This is intentionally a release-proof artifact adapter, not a new behavioral
scenario implementation.

## Phase 3 Baselines

Accepted baselines should be normalized proof artifacts, not raw stdout/stderr.
Raw artifacts stay attached for debugging and agent review.
Only `green` proof artifacts can be accepted as baselines. `yellow` means the
proof is incomplete or insufficiently trusted, so it must remain visible as a
release-watch gap instead of becoming `upgrade_allowed` after a matching diff.

Proposed layout for a caller such as Sauron:

```text
provider-release-proofs/{provider}/{scenario_id}/
  accepted.json
  versions/{provider_version}/
    proof.json
    raw/
    normalized/
```

Manual acceptance is required the first time a provider/scenario is trusted.
After that, release-watch can compare the new proof against the accepted proof.
If the underlying canary behavior changes meaningfully, bump `scenario_version`
before comparing new candidates to old accepted baselines.

Initial utility:

```bash
make provider-release-proof-accept \
  PROOF=/tmp/proof.json \
  BASELINE_ROOT=/data/provider-release-proofs \
  ARTIFACT=/tmp/baseline-acceptance.json
```

Equivalent direct script:

```bash
scripts/qa/provider-release-proof-baseline.py accept \
  --proof /tmp/proof.json \
  --baseline-root /data/provider-release-proofs \
  --json
```

This writes:

```text
{baseline_root}/{provider}/{scenario_id}/
  accepted.json
  versions/{provider_version}/proof.json
  versions/{provider_version}/artifacts/
```

The utility copies referenced artifact files when they exist, so raw
stdout/stderr and normalized contract artifacts stay available after acceptance.

## Phase 4 Differential Runs

The release gate should eventually run:

```text
accepted provider version -> provider-release-proof A
candidate provider version -> provider-release-proof B
diff A.normalized vs B.normalized
```

Do not diff raw logs byte-for-byte. Ignore timestamps, UUIDs, absolute paths,
token counts, streaming chunk boundaries, and model prose unless the scenario
uses an explicit marker string.

Initial utility:

```bash
make provider-release-proof-diff \
  CANDIDATE=/tmp/new-proof.json \
  BASELINE_ROOT=/data/provider-release-proofs \
  ARTIFACT=/tmp/proof-diff.json
```

Equivalent direct script:

```bash
scripts/qa/provider-release-proof-baseline.py diff \
  --candidate /tmp/new-proof.json \
  --baseline-root /data/provider-release-proofs \
  --json
```

For direct old/new comparison without an accepted store:

```bash
scripts/qa/provider-release-proof-baseline.py diff \
  --base /tmp/old-proof.json \
  --candidate /tmp/new-proof.json \
  --json
```

The first comparison view excludes `provider_version`; version is metadata and
should not by itself count as contract drift.

## Next Work

1. Add exact-version staging for Claude Code and Codex release packages.
2. Add Claude managed-session binding proof beyond no-token launch shape.
3. Decide whether Antigravity real-agy send belongs in scheduled CI or remains
   an opt-in live-token proof.
4. Accept the first real OpenCode proof baseline from a known-good version.
5. Start old/new differential proof runs from accepted baselines.
