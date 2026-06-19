# Provider Release Proof Runbook

This is the operator path for proving upstream provider CLI releases against
Longhouse. The supported release-proof providers are Claude Code, Codex/OpenAI,
OpenCode, and Antigravity. Gemini is not a release-proof provider; legacy
`gemini` parser fixtures remain import compatibility evidence only.

The design matrix lives in `docs/specs/provider-release-proof.md`. Use this page
when running, accepting, diffing, or interpreting proof artifacts.

## Baseline Stores

Use the store that matches the caller:

- Longhouse local command default: `.provider-release-proofs`
- David dogfood accepted store: `~/.local/share/longhouse/provider-release-proofs`
- Sauron production container store: `/data/provider-release-proofs`

Sauron release-envelope tooling also honors `AGENT_RELEASE_PROOF_BASELINE_ROOT`
and the legacy `AGENT_RELEASE_ENVELOPE_BASELINE_ROOT`.

## Run A Proof

From `~/git/zerg/longhouse`:

```bash
make provider-release-proof \
  PROVIDER=opencode \
  PROVIDER_BIN=/path/to/opencode \
  ARTIFACT=/tmp/proof.json \
  EVIDENCE_ROOT=/tmp/proof-evidence \
  SOURCE_REVIEW_STATUS=pass \
  SOURCE_REVIEW_NOTE="Reviewed upstream release notes and no Longhouse contract change was found."
```

Direct script equivalent:

```bash
scripts/qa/provider-release-proof.py \
  --provider opencode \
  --provider-bin /path/to/opencode \
  --artifact /tmp/proof.json \
  --evidence-root /tmp/proof-evidence \
  --source-review-status pass \
  --source-review-note "Reviewed upstream release notes and no Longhouse contract change was found." \
  --json
```

For Codex, optional Make variables enable the deeper lanes:

```bash
CODEX_RUN_FAKE_APP_SERVER=1 \
CODEX_RUN_RAW_FRESH_REMOTE=1 \
CODEX_RUN_MANAGED_TUI_ATTACH=1 \
CODEX_RUN_DETACHED_UI=1 \
CODEX_RUN_MANAGED_LIVE_SEND=1 \
make provider-release-proof \
  PROVIDER=codex \
  PROVIDER_BIN=/path/to/codex \
  ARTIFACT=/tmp/codex-proof.json \
  EVIDENCE_ROOT=/tmp/codex-proof-evidence \
  SOURCE_REVIEW_STATUS=pass \
  CODEX_API_URL=https://longhouse.example.com \
  CODEX_AGENTS_TOKEN=...
```

Today the managed Codex bridge lanes need Runtime Host credentials. Without
`CODEX_API_URL` and `CODEX_AGENTS_TOKEN`, those lanes must report
`status=not_run` and
`failure_code=managed_bridge_credentials_missing`; that is a coverage gap, not
an upstream break. `CODEX_RUN_MANAGED_LIVE_SEND=1` spends a real managed Codex
turn and records `operation_evidence.send_input` at `level=live_token` only
after the turn completes and transcript/state evidence contains the unique
canary marker. Proofs with this flag use scenario
`codex-managed-live-send-release-proof-v1`; the default
`codex-release-proof-v1` remains the no-token managed/protocol baseline. Do not
accept a Codex baseline while the managed attach, detached UI, or live-send
lanes are still missing if the baseline is intended to protect those surfaces.

Sauron release-watch reuses `AGENT_RELEASE_LONGHOUSE_API_URL` and
`AGENT_RELEASE_LONGHOUSE_AGENTS_TOKEN` for the same Codex managed bridge proof
when `AGENT_RELEASE_CODEX_CANARY_LIVE=1`.

Run the no-spend preflight before trying to accept a live-send baseline:

```bash
CODEX_RUN_MANAGED_LIVE_SEND=1 \
PREFLIGHT_ONLY=1 \
make provider-release-proof \
  PROVIDER=codex \
  ARTIFACT=/tmp/codex-live-preflight.json
```

`artifact_kind=provider_release_proof_preflight` with
`failure_code=provider_release_proof_prerequisites_missing` means the live proof
scenario is correctly selected, but the Runtime Host URL/token or binary
prerequisites are not present. The preflight records only pass/fail presence
checks and never includes token material.

Antigravity has an optional live-token send proof:

```bash
ANTIGRAVITY_RUN_REAL_AGY_SEND=1 \
make provider-release-proof \
  PROVIDER=antigravity \
  PROVIDER_BIN=/path/to/agy \
  SOURCE_REVIEW_STATUS=pass \
  ARTIFACT=/tmp/antigravity-proof.json \
  EVIDENCE_ROOT=/tmp/antigravity-proof-evidence
```

This spends a real `agy --print` turn through
`provider-control-e2e-canary.py --antigravity-real-agy-send` and attaches the
resulting `operation_evidence.send_input` to the release proof. Proofs with this
flag use scenario `antigravity-real-agy-send-release-proof-v1`; the default
`antigravity-release-proof-v1` remains the no-token hook/plugin baseline.
Accept this as a baseline only after confirming the artifact shows
`level=live_token` and the model-visible marker came from the injected
Longhouse inbox message.

## Read A Proof

Key top-level fields:

- `verdict=green`: the proof ran with enough evidence and the candidate may be
  compared or accepted.
- `verdict=yellow`: the proof found an honest gap, missing baseline, missing
  credential, or insufficient coverage. Do not accept it as a trusted baseline.
- `verdict=red`: block the upgrade until the failure is understood.
- `artifact_kind=provider_release_proof_preflight`: prerequisite-only artifact;
  use it for readiness checks, not baseline acceptance.
- `failure_code`: the actionable reason for yellow/red.
- `source_canary_returncode`: `0` for green/yellow source canaries, `1` for red.
- `artifacts`: raw stdout/stderr plus normalized comparable proof files.

Important artifact pointers:

- `source_artifact`: the wrapped provider-specific canary output.
- `normalized_contract`: compact comparable release-proof shape.
- `provider_contract`: Longhouse managed-provider contract fields.
- `operation_evidence`: launch/send/attach operation evidence.
- `session_projection`: projected session behavior, or explicit `not_captured`.

## Accept A Baseline

Only accept a `green` proof after reviewing the raw evidence. Acceptance archives
the proof and referenced artifacts so future status/diff runs do not depend on
temporary directories.

```bash
make provider-release-proof-accept \
  PROOF=/tmp/proof.json \
  BASELINE_ROOT="$HOME/.local/share/longhouse/provider-release-proofs" \
  ARTIFACT=/tmp/baseline-acceptance.json
```

Check the accepted baseline:

```bash
make provider-release-proof-status \
  PROVIDER=opencode \
  SCENARIO_ID=opencode-release-proof-v1 \
  BASELINE_ROOT="$HOME/.local/share/longhouse/provider-release-proofs" \
  ARTIFACT=/tmp/baseline-status.json
```

`failure_code=baseline_missing` means there is no accepted baseline yet.
`failure_code=baseline_artifacts_missing` means an accepted baseline exists but
one or more archived evidence files no longer resolve; repair the baseline store
before trusting release-watch output.
`failure_code=accepted_baseline_not_green` means the accepted store was edited or
corrupted; treat it as red integrity failure and recopy or re-accept the baseline
from reviewed green evidence.

## Diff A Candidate

Diff a fresh candidate against the accepted baseline:

```bash
make provider-release-proof-diff \
  CANDIDATE=/tmp/proof.json \
  BASELINE_ROOT="$HOME/.local/share/longhouse/provider-release-proofs" \
  ARTIFACT=/tmp/proof-diff.json
```

Diff explicit old/new proof artifacts:

```bash
make provider-release-proof-diff \
  BASE=/tmp/old-proof.json \
  CANDIDATE=/tmp/new-proof.json \
  ARTIFACT=/tmp/old-new-proof-diff.json
```

`diff.status=match` with `verdict=green` means no normalized contract drift was
found. `diff.status=different` with `verdict=red` is a release-risk signal.
`diff.status=not_compared` is usually a yellow setup gap such as
`baseline_missing`.

## Promote To Sauron

Promote a local accepted provider baseline to the Sauron production container
only after a green proof, green status, and green diff.

```bash
COPYFILE_DISABLE=1 tar --no-xattrs \
  -C "$HOME/.local/share/longhouse/provider-release-proofs" \
  -cf - opencode \
  | ssh clifford "docker exec -u 0 -i sauron sh -lc 'mkdir -p /data/provider-release-proofs && tar -C /data/provider-release-proofs -xf -'"
```

Verify from inside the Sauron container:

```bash
ssh clifford "docker exec sauron sh -lc 'cd /data/jobs && python3 -m jobs.agents.release_envelope status --provider opencode --scenario-id opencode-release-proof-v1 --baseline-root /data/provider-release-proofs --json'"
```

The status should be `verdict=green`, `accepted=true`, and
`missing_archived_artifacts=[]`.

## Sauron Email Interpretation

Routine release digests should skip the inbox and live under a label/archive
lane. Inbox alerts are for structured provider-status evidence:

- `red`: critical release risk; block or pin until investigated.
- `yellow` plus `baseline_missing`: expected while a provider lacks an accepted
  baseline; useful but not inbox-worthy by itself.
- `yellow` plus `insufficient_coverage`: proof gap; improve the proof lane or
  keep the release untrusted.
- `yellow` plus `baseline_artifacts_missing`: actionable store problem; repair
  or recopy the accepted baseline.
- `yellow` plus `managed_bridge_credentials_missing`: Codex managed bridge proof
  was not configured; not an upstream break.
- `golden_envelope`: Sauron compared the candidate to an accepted known-good
  baseline.
- `release_differential`: Sauron staged old and new release assets and compared
  their normalized Longhouse proof artifacts directly.

The Sauron alert path currently treats `baseline_missing` and
`insufficient_coverage` as non-actionable yellows for inbox purposes. Other
yellow failure codes should reach the inbox as warnings.

Codex Runtime Host tokens are read from environment variables and passed to
nested proof commands through `CODEX_AGENTS_TOKEN`; they should not appear in
argv, raw command evidence, or published fallback artifacts.

## Current Provider State

- OpenCode: accepted baseline `opencode-release-proof-v1`, provider version
  `opencode 1.16.2`.
- Claude Code: accepted scoped baseline `claude-release-proof-v1`, provider
  version `claude 2.1.161`.
- Antigravity: accepted scoped baseline `antigravity-release-proof-v1`, provider
  version `agy 1.0.8`.
- Codex/OpenAI: accepted baseline `codex-release-proof-v1`, provider version
  `codex-cli 0.139.0`.
- Codex/OpenAI live-send: no accepted baseline yet for
  `codex-managed-live-send-release-proof-v1`.
- Antigravity real-agy send: no accepted baseline yet for
  `antigravity-real-agy-send-release-proof-v1`.

## Promotion Checklist

1. Run the real proof with the exact provider binary and version.
2. Review raw evidence and confirm the proof is `green`.
3. Accept the proof into the intended baseline store.
4. Run baseline status and confirm no archived artifacts are missing.
5. Rerun the proof and diff it against the accepted baseline.
6. Copy the accepted baseline to `/data/provider-release-proofs` when Sauron
   should trust it.
7. Verify Sauron status from inside the container.
8. Update `docs/specs/provider-release-proof-coverage.json` and
   `docs/specs/provider-release-proof.md` if baseline-backed coverage changed.

## Focused Validation

Use these for Longhouse proof-lane changes:

```bash
make validate-provider-cli-canaries
python3 scripts/tests/provider-release-proof-coverage.test.py
python3 scripts/tests/provider-release-proof.test.py
python3 scripts/tests/provider-release-proof-baseline.test.py
python3 scripts/tests/provider-release-proof-make.test.py
```

Use these for Sauron release-watch or envelope changes from `~/git/sauron/jobs`:

```bash
PYTHONPATH=../runtime:. uv run \
  --with pytest \
  --with aiohttp \
  --with boto3 \
  --with markdown-it-py \
  python -m pytest \
  tests/test_agent_release_envelope.py \
  tests/test_agent_release_provider_status.py \
  -q
```
