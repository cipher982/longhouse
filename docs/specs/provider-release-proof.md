# Provider Release Proof

**Status:** Phase 1 inventory + Longhouse proof/baseline/diff entrypoints; OpenCode, scoped Claude, and scoped Antigravity release-proof baselines accepted
**Owner:** David
**Last updated:** 2026-06-19

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
- parser goldens for Claude, Codex, and Antigravity legacy JSON imports:
  `engine/tests/golden_parser_contract.rs`
- provider release/live/control canary scripts under `scripts/qa/`
- Sauron release-watch provider-status publication

What is missing:

- an accepted release-proof baseline for Codex
- raw-to-normalized proof fixtures for all release-sensitive surfaces
- full managed-session/live-token proof for every release-sensitive surface
- scheduled old/new differentials from the accepted baseline store rather than
  only candidate or directly staged old/new artifacts

## Audit Snapshot - 2026-06-18

This snapshot reflects the Longhouse audit through `8b5426a84`, Sauron jobs
`6f1a212`, and the 2026-06-19 local OpenCode, scoped Claude, and scoped
Antigravity baseline promotions after Gemini was removed as a release-watch
provider and Antigravity became the canonical Google lane. Later Sauron
surface-target commits are unrelated to provider release-proofing; the
release-watch/proof scope remains Claude Code, Codex/OpenAI, OpenCode, and
Antigravity.

Machine-validated coverage map:

| Metric | Count |
| --- | ---: |
| Providers | 4 |
| Contract surfaces per provider | 13 |
| Total provider/surface rows | 52 |
| Covered `yes` | 11 |
| Covered `partial` | 35 |
| Covered `no` | 6 |
| Rows running in Longhouse CI | 41 |
| Rows running in Sauron release-watch | 29 |
| Rows with accepted parser-fixture baselines | 4 |
| Rows with accepted release-proof baselines | 15 |

Provider shape:

| Provider | Yes | Partial | No | CI rows | Sauron rows | Release baselines |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Claude Code | 2 | 11 | 0 | 11 | 5 | 3 |
| Codex/OpenAI | 4 | 8 | 1 | 11 | 8 | 0 |
| OpenCode | 4 | 7 | 2 | 11 | 11 | 9 |
| Antigravity | 1 | 9 | 3 | 8 | 5 | 3 |

Known uncovered surfaces:

- Codex/OpenAI: live-token behavior.
- OpenCode: tool/tool-result shape and live-token behavior.
- Antigravity: interrupt/abort/steer, reattach/resume, and tool/tool-result
  shape.

Implication: CI is meaningful for parser, wrapper, profile-canary, and several
no-token live surfaces. OpenCode has an accepted known-good release-proof
baseline for its no-token server/control proof, Claude has an accepted scoped
no-token baseline for binary identity, channel/status shape, and launch
flag/PTY shape, and Antigravity has an accepted scoped no-token baseline for
binary identity, plugin/global hook shape, and hook-inbox launch mechanics.
This is still not a complete release gate: Codex has no accepted release-proof
baseline, Claude still lacks managed-session binding and live-token proof,
Antigravity still lacks model-visible send/reattach/tool proof, and OpenCode
still lacks tool/tool-result and live-token proof.

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
For OpenCode, the current `release_proof` baseline is a `live_no_token`
baseline; it protects provider server/API/control-shape drift, not
model-visible response quality. For Claude, the current `release_proof`
baseline is narrower: it protects CLI binary identity, channel/status shape,
and launch flag/PTY shape, not model-visible send, steer, transcript binding,
or resume semantics. For Antigravity, the current `release_proof` baseline is
also scoped: it protects binary identity, plugin/global hook shape, and
hook-inbox launch mechanics, not model-visible send, reattach, tools, or
live-token behavior.

## Phase 1 Coverage Map

The machine-checkable map lives in
`docs/specs/provider-release-proof-coverage.json` and is validated by
`scripts/tests/provider-release-proof-coverage.test.py`, which runs under
`make validate-provider-cli-canaries`. The tables below are the readable
summary; update the JSON first when a provider/surface changes.

### Claude Code

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron stages npm-sourced `@anthropic-ai/claude-code@version` into an isolated artifact root, passes `.bin/claude` to profile/live canaries, and wraps it in Longhouse proof/differential envelopes | isolated npm package | no Longhouse CI; Sauron tests cover it | yes for npm releases | no | yes if staging/version match fails or old/new proof drift is red |
| binary identity | yes | `provider-live-canary --provider claude`, `provider-release-profile-canary.py` | live_no_token or fake | `validate-provider-cli-canaries` | yes, through provider status | release-proof yes (`claude-release-proof-v1`, 2.1.161) | yes if binary missing/version fails |
| auth/status shape | partial | `provider-live-canary --provider claude` binary/auth/channel checks | live_no_token | `validate-provider-cli-canaries` | yes if live proof configured | release-proof yes (`claude-release-proof-v1`, 2.1.161) | yes if red |
| launch managed session | partial | `provider-control-e2e-canary.py`, `test_claude_channel_launch_cli.py`, Sauron proof/diff for no-token launch flag shape | hermetic plus exact npm package shape | `validate-provider-cli-canaries`, `make test`; Sauron tests cover release-watch proof wiring | profile/live plus proof/diff for npm releases | release-proof yes for no-token launch/PTY shape (`claude-release-proof-v1`, 2.1.161) | partial |
| session id/path binding | partial | `test_claude_channel_bridge.py`, hook/session tests | hermetic | `make test` | no dedicated baseline | no | partial |
| transcript/log parse | yes | engine Claude golden + adversarial parser tests | fixture | `make test-engine` | source review only | parser fixture yes; release-proof no | yes for parser drift |
| ingest into Longhouse | partial | shipper E2E, Claude hook/outbox tests | fixture/hermetic | `make test`, `make test-shipper-e2e` | no dedicated release proof | no | partial |
| timeline/session projection | partial | session capability/messages/view tests | hermetic | `make test` | no | no | partial |
| send input | partial | managed-local chat/channel bridge tests | hermetic | `make test` | live proof only if configured | no | partial |
| interrupt/abort/steer | partial | Claude interrupt/steer channel tests; managed Claude POC is manual/live | hermetic + manual live_token | `make test`; manual target | no scheduled baseline | no | partial |
| reattach/resume | partial | channel bridge resume/state tests | hermetic | `make test` | no dedicated baseline | no | partial |
| tool/tool-result shape | partial | parser/tool-result tests cover transcript shapes | fixture/hermetic | `make test`, `make test-engine` | source review only | parser fixture yes; release-proof no | partial |
| live-token behavior | partial | `make managed-claude-poc` | live_token manual | no normal CI | no | no | no |

Claude risk: high. Closed source and release notes are not enough. Sauron now
stages exact npm package versions and runs Longhouse proof/differential
artifacts against an accepted scoped no-token baseline. Full managed-session
binding, transcript, send/steer, resume, and live-token proof are still missing.

### Codex

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron stages the exact Codex GitHub release asset, passes it to `codex-provider-release-canary.py`, and now wraps the staged release in a Longhouse proof/diff candidate envelope; Longhouse tests the binary override path | real release asset | no Longhouse CI for asset staging; canary override runs in CI | yes for source-reviewed GitHub releases | no | yes if staging/version match fails |
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

Codex is the strongest existing provider lane. Sauron now produces a
Longhouse-owned proof artifact and proof-baseline diff for source-reviewed
staged release assets, but it still needs the first accepted real baseline
before old/new differential results can go green.

Local smoke evidence, 2026-06-18: Codex `0.139.0` with
`CODEX_RUN_FAKE_APP_SERVER=1` and `CODEX_RUN_RAW_FRESH_REMOTE=1` produced a
yellow proof with real `tail_output` protocol fingerprint evidence. This is
useful release evidence, but not yet enough for baseline acceptance because
launch/reattach managed bridge evidence remains missing without the managed
bridge canaries.

Local smoke evidence, 2026-06-19: Codex `codex-cli 0.139.0` with the same
fake app-server plus raw-fresh-remote lane still stayed yellow. One run timed
out after the app-server initialized, started a thread, accepted `turn/start`,
and waited for completion; the canary now preserves protocol fingerprints even
on that failure path. A rerun passed raw-fresh-remote and captured stable
`initialize`, `thread/resume`, `turn/start`, `thread/started`, and
`turn/completed` protocol fingerprints. This still should not be accepted as a
baseline because source review was `not_run` and the managed TUI attach plus
detached-UI lanes require a Runtime Host `--api-url` and `--agents-token`.

### OpenCode

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron OpenCode release asset staging | real release asset | Sauron tests | yes | no | yes if staging fails |
| binary identity | yes | `provider-live-canary --provider opencode` | live_no_token or fake | `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | yes |
| auth/status shape | partial | server health/auth/doc checks | live_no_token | `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | yes |
| launch managed session | yes | provider live canary server/session checks; channel CLI tests | live_no_token + hermetic | `validate-provider-cli-canaries`, `make test` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | yes |
| session id/path binding | partial | OpenCode bridge/channel state tests plus provider-live sidecar classification | hermetic/live_no_token | `make test`, `validate-provider-cli-canaries` | provider-live sidecar classification | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | partial |
| transcript/log parse | partial | live canary `session.messages` marker; no engine parser golden | live_no_token | `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | partial |
| ingest into Longhouse | partial | provider-live session classification and route tests | hermetic/live_no_token | `make test`, `validate-provider-cli-canaries` | yes | no | partial |
| timeline/session projection | partial | session capability/view tests plus provider-live session projection captured by `provider_release_proof` | hermetic/live_no_token | `make test`, `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | partial |
| send input | yes | provider-live canary `prompt_async` noReply marker | live_no_token | `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | yes |
| interrupt/abort/steer | partial | provider-live abort endpoint; steer unsupported | live_no_token | `validate-provider-cli-canaries` | yes for interrupt | release-proof yes for abort (`opencode-release-proof-v1`, 1.16.2); steer remains unsupported | yes for abort, no for steer |
| reattach/resume | yes | provider-live process restart/session recovery + attach shape | live_no_token | `validate-provider-cli-canaries` | yes | release-proof yes (`opencode-release-proof-v1`, 1.16.2) | yes |
| tool/tool-result shape | no | no OpenCode parser/tool result golden found | none | no | no | no | no |
| live-token behavior | no | manifest marks prompt/abort proof as next release lane | none | no | no | no | no |

OpenCode is the first accepted release-proof baseline. On 2026-06-19,
`opencode 1.16.2` produced a green `opencode-release-proof-v1` artifact and a
fresh rerun diffed green/match against the accepted baseline. The accepted
local dogfood baseline is under
`~/.local/share/longhouse/provider-release-proofs/opencode/opencode-release-proof-v1/`.
The archived artifacts include source stdout/stderr, normalized contract,
provider contract, operation evidence, and session projection. This proves the
no-token server/API/control-shape lane; it does not prove OpenCode
tool/tool-result transcript shape or live-token model-visible behavior.

### Antigravity

| Surface | Covered | Evidence | Boundary | CI | Sauron release-watch | Baseline | Actionable today |
| --- | --- | --- | --- | --- | --- | --- | --- |
| install/stage exact version | partial | Sauron stages the exact Antigravity GitHub release asset, passes it to profile/live canaries, and wraps it in Longhouse proof/differential envelopes | real release asset | no Longhouse CI; Sauron tests cover it | yes for source-reviewed releases | no | yes if staging/version match fails or old/new proof drift is red |
| binary identity | yes | `provider-live-canary --provider antigravity`, profile canary | live_no_token or fake | `validate-provider-cli-canaries` | yes | release-proof yes (`antigravity-release-proof-v1`, 1.0.8) | yes |
| auth/status shape | partial | version/help/plugin/global hook checks | live_no_token | `validate-provider-cli-canaries` | yes | release-proof yes (`antigravity-release-proof-v1`, 1.0.8) | partial |
| launch managed session | partial | hook/plugin plus hook-inbox claim checks | live_no_token/hermetic | `validate-provider-cli-canaries` | profile/live only | release-proof yes for no-token hook-inbox shape (`antigravity-release-proof-v1`, 1.0.8) | partial |
| session id/path binding | partial | hook binding tests | hermetic | `make test` | no | no | partial |
| transcript/log parse | partial | hook transcript binding tests | hermetic | `make test` | no | no | partial |
| ingest into Longhouse | partial | hook outbox/runtime tests | hermetic | `make test` | no | no | partial |
| timeline/session projection | partial | session capabilities for Antigravity transport | hermetic | `make test` | no | no | partial |
| send input | partial | `provider-control-e2e-canary.py --antigravity-real-agy-send`; Sauron release-watch now preserves Longhouse proof/differential evidence from staged `agy` | live_token when explicitly run; release proof uses current live canary maturity | wrapper test in CI uses fake agy | profile/live plus proof/diff | no | partial |
| interrupt/abort/steer | no | unsupported in manifest | none | no | no | no | no |
| reattach/resume | no | unsupported in manifest | none | no | no | no | no |
| tool/tool-result shape | no | no provider transcript parser golden found | none | no | no | no | no |
| live-token behavior | partial | real agy send canary exists but is not a scheduled CI/release lane | live_token manual/configured | fake-wrapper CI only | no | no | no |

Antigravity should stay narrow: on 2026-06-19, `agy 1.0.8` produced a green
`antigravity-release-proof-v1` artifact and a fresh rerun diffed green/match
against the accepted baseline. The accepted local dogfood baseline is under
`~/.local/share/longhouse/provider-release-proofs/antigravity/antigravity-release-proof-v1/`.
This proves the no-token binary/plugin/global-hook/hook-inbox contract. It
does not prove model-visible send, reattach/resume, tool/tool-result shape, or
live-token behavior. The next useful step is proving the hook inbox actually
changes the model-visible turn, then keeping unsupported operations explicit.

### Legacy Google JSON Imports

Gemini CLI is no longer a Longhouse release-proof provider. Antigravity is the
canonical Google provider lane for launch/control/release-watch. Some older
Google CLI session JSON files still use `type: "gemini"` and live under
Antigravity legacy JSON fixtures. Those fixtures remain import compatibility
evidence for parser and shipper behavior, but they do not create a
`provider-release-proof --provider gemini` lane and Sauron should not publish a
`gemini.json` provider-status artifact.

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
- `CODEX_RUN_FAKE_APP_SERVER`, `CODEX_RUN_RAW_FRESH_REMOTE`,
  `CODEX_RUN_MANAGED_TUI_ATTACH`, and `CODEX_RUN_DETACHED_UI` enable opt-in
  Codex canary lanes.

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
    "normalized_contract": "...",
    "provider_contract": "...",
    "operation_evidence": "...",
    "session_projection": "..."
  }
}
```

The normalized artifact files are separate on purpose:

- `normalized_contract` is the compact comparable proof shape.
- `provider_contract` is the managed-provider contract surface used for the
  proof.
- `operation_evidence` is the normalized evidence map for launch/send/attach
  operations.
- `session_projection` is captured when a source canary emits it; otherwise it
  is an explicit `not_captured` artifact so the gap is visible in accepted
  baselines.

Current implementation wraps existing source canaries:

- Claude/OpenCode/Antigravity: `scripts/qa/provider-live-canary.py`
- Codex: `scripts/qa/codex-provider-release-canary.py`

Claude npm release-watch ticks now have exact-version package staging in Sauron:
`@anthropic-ai/claude-code@<version>` is installed under the release artifact
root and the staged `.bin/claude` path is passed to Longhouse profile/live
canaries. Sauron also runs the Longhouse proof wrapper and, when `prev_tag`
installs, an explicit old/new proof diff. This proves package staging, binary
identity, and normalized no-token contract-shape drift for npm-sourced release
events, but not an accepted baseline or full managed-session live-token proof
yet.

Claude normalization preserves no-token launch-contract shape: missing launch
flags from `claude --help`, development-channel status/missing flags, and
detached PTY wrapper status/platform. Failure codes and reasons stay in the
typed Claude block so a dev-channel contract break differs from local PTY
environment failure in old/new diffs. This is not yet a full managed-session
launch proof.

Codex normalization preserves source-review status, binary identity presence,
operation evidence, canary statuses/reasons, and stable protocol fingerprints
from `raw_fresh_remote` while dropping noisy path fields. A protocol fingerprint
status change such as `ok` -> `missing` is contract drift signal. Sauron now
calls this proof wrapper for staged Codex release assets and attaches the
Longhouse diff result as `canaries.golden_envelope`; `baseline_missing` remains
yellow evidence until a real green Codex proof is accepted. Sauron also stages
the previous Codex release asset and runs an explicit Longhouse
`--base/--candidate` proof diff when both old/new assets are available; that
result is `canaries.release_differential`. Red old/new proof drift is a
top-level release-risk signal, while missing accepted baselines remain separate
yellow evidence.

Antigravity release-watch now follows the same Longhouse proof wrapper shape
for staged `agy` release assets. It publishes `canaries.golden_envelope` and,
when the previous release asset stages, `canaries.release_differential`; red
old/new proof drift is a top-level release-risk signal. The diff compares
normalized Longhouse canary contract fields, not arbitrary binary bytes, so two
different binaries with identical live-canary shape still match. This does not
by itself make Antigravity green enough for full model-visible send-input
confidence, because the scheduled release proof still stops short of the
manual live-token `agy send` scenario.

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
Acceptance archives the referenced raw and normalized artifact files, including
`provider_contract`, `operation_evidence`, and `session_projection`, so later
status/diff runs do not depend on temporary proof directories.

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

Baseline status is also machine-readable:

```bash
make provider-release-proof-status \
  PROVIDER=opencode \
  SCENARIO_ID=opencode-release-proof-v1 \
  BASELINE_ROOT=/data/provider-release-proofs \
  ARTIFACT=/tmp/baseline-status.json
```

Equivalent direct script:

```bash
scripts/qa/provider-release-proof-baseline.py status \
  --provider opencode \
  --scenario-id opencode-release-proof-v1 \
  --baseline-root /data/provider-release-proofs \
  --json
```

This emits `accepted`, `provider_version`, `accepted_at`,
`archived_artifacts`, and `missing_archived_artifacts`, so release-watch and CI
can distinguish "the proof lane exists" from "a known-good baseline is actually
accepted and still has its evidence files."

## Phase 4 Differential Runs

The release gate should eventually run:

```text
accepted provider version -> provider-release-proof A
candidate provider version -> provider-release-proof B
diff A.required_contract_fields vs B.required_contract_fields
```

Do not diff raw logs byte-for-byte. Ignore timestamps, UUIDs, absolute paths,
token counts, streaming chunk boundaries, and model prose unless the scenario
uses an explicit marker string.

The initial diff utility compares the embedded `normalized` contract plus the
stable portions of the normalized artifact files:

- `provider_contract`: provider and contract operations.
- `operation_evidence`: operation status, level, canary, and failure code.
- `session_projection`: captured/not-captured status plus stable check and
  operation statuses. Volatile provider session ids, sidecar paths, marker
  hashes, and elapsed timings are preserved in artifacts but ignored for drift.

If a declared comparable artifact is missing, unreadable, or malformed, the
diff fails closed with `provider_release_proof_comparable_artifacts_unavailable`
instead of omitting that plane and returning a false match.

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

1. Add Claude managed-session binding proof beyond no-token launch shape.
2. Promote Codex beyond `insufficient_coverage` by running and hardening the
   managed TUI attach, detached UI, and raw-fresh remote lanes.
3. Fix the Antigravity hook-inbox import failure observed in the 2026-06-19
   readiness probe before accepting any Antigravity baseline.
4. Decide whether Antigravity real-agy send belongs in scheduled CI or remains
   an opt-in live-token proof.
5. Accept real Codex and Antigravity baselines once their proof lanes are green
   enough to trust.
6. Add model-visible live-token proof for the surfaces still marked partial.
