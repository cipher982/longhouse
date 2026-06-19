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

OpenCode has an optional real-tool proof:

```bash
OPENCODE_RUN_REAL_TOOL=1 \
make provider-release-proof \
  PROVIDER=opencode \
  PROVIDER_BIN=/path/to/opencode \
  PROVIDER_VERSION="opencode 1.16.2" \
  SOURCE_REVIEW_STATUS=pass \
  ARTIFACT=/tmp/opencode-tool-proof.json \
  EVIDENCE_ROOT=/tmp/opencode-tool-proof-evidence
```

This spends a real `opencode run --format json` turn through
`provider-control-e2e-canary.py --opencode-run-real-tool` and attaches
`operation_evidence.transcript_binding` at `level=live_token`. Proofs with this
flag use scenario `opencode-real-tool-release-proof-v1`; the default
`opencode-release-proof-v1` remains the no-token server/control baseline.
Accept this as a baseline only after confirming the artifact shows a completed
`bash` tool event with a non-empty `callID`, structured command input, and
exact marker output plus a same-session `DONE` text event. The real-run timeout
has a 45 second minimum guard. Sauron release-watch can request this same
scenario for OpenCode golden-envelope and old/new differential checks with
`AGENT_RELEASE_OPENCODE_REAL_TOOL=1`; production Sauron now enables this gate
after promoting the accepted baseline.

For Codex, optional Make variables enable the deeper lanes:

```bash
CODEX_RUN_FAKE_APP_SERVER=1 \
CODEX_RUN_RAW_FRESH_REMOTE=1 \
CODEX_RUN_MANAGED_TUI_ATTACH=1 \
CODEX_RUN_DETACHED_UI=1 \
CODEX_RUN_MANAGED_LIVE_SEND=1 \
CODEX_RUN_MANAGED_LIVE_INTERRUPT=1 \
CODEX_RUN_REAL_TOOL=1 \
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
an upstream break. When accepting a profile baseline, run one of
`CODEX_RUN_MANAGED_LIVE_SEND`, `CODEX_RUN_MANAGED_LIVE_INTERRUPT`, or
`CODEX_RUN_REAL_TOOL` at a time unless `SCENARIO_ID` is explicitly overriding
the proof bucket.

`CODEX_RUN_MANAGED_LIVE_SEND=1` spends a real managed Codex
turn and records `operation_evidence.send_input` at `level=live_token` only
after the turn completes and transcript/state evidence contains the unique
canary marker. Proofs with this flag use scenario
`codex-managed-live-send-release-proof-v1`; the default
`codex-release-proof-v1` remains the no-token managed/protocol baseline. Do not
accept a Codex baseline while the managed attach, detached UI, or live-send
lanes are still missing if the baseline is intended to protect those surfaces.

`CODEX_RUN_MANAGED_LIVE_INTERRUPT=1` spends a real managed Codex turn and
records `operation_evidence.interrupt` at `level=live_token` only after
`codex-bridge interrupt` succeeds and bridge state reaches `interrupted` or
`cancelled`. Proofs with this flag use scenario
`codex-managed-live-interrupt-release-proof-v1`. An accepted baseline exists
for `codex-cli 0.139.0`. Sauron can request this same proof/diff scenario with
`AGENT_RELEASE_CODEX_MANAGED_LIVE_INTERRUPT=1`. Production Sauron now enables
that gate with Runtime Host credentials configured.

`CODEX_RUN_REAL_TOOL=1` spends a real local `codex exec --json` turn and records
`operation_evidence.run_once` plus `operation_evidence.transcript_binding` at
`level=live_token` after a completed `command_execution` event emits the exact
marker output and a DONE `agent_message`. Proofs with this flag use scenario
`codex-real-tool-release-proof-v1`.

Sauron release-watch can request that same Codex real-tool scenario for
golden-envelope and old/new differential checks with
`AGENT_RELEASE_CODEX_REAL_TOOL=1`. Production Sauron does not run this
token-spending lane by default.

Sauron release-watch reuses `AGENT_RELEASE_CODEX_LONGHOUSE_API_URL` and
`AGENT_RELEASE_CODEX_LONGHOUSE_AGENTS_TOKEN` for the same Codex managed bridge
proof when `AGENT_RELEASE_CODEX_CANARY_LIVE=1`, falling back to the global
`AGENT_RELEASE_LONGHOUSE_*` variables if provider-specific values are absent.
Production Sauron has the Codex provider-specific live-send variables
configured as of 2026-06-19.

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

Sauron can pass this same scenario through Antigravity release-watch by setting
`AGENT_RELEASE_ANTIGRAVITY_REAL_AGY_SEND=1`. The reviewed green
`antigravity-real-agy-send-release-proof-v1` baseline for `agy 1.0.10` was
accepted on 2026-06-19. Production Sauron now enables this gate after promoting
the accepted baseline, so release-watch runs the real-send proof/diff scenario
for source-reviewed Antigravity releases.

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

Promote local accepted provider baselines to the Sauron production container
only after a green proof, green status, and green diff. Copy the full baseline
store, not just `accepted.json`; archived artifacts under `versions/` are part
of the integrity check.

Here "promote to Sauron" means copying accepted known-good baseline artifacts
into the production guard store. It does not by itself enable any
token-spending release-watch lane.

```bash
COPYFILE_DISABLE=1 tar \
  --exclude='._*' \
  --exclude='.DS_Store' \
  -C "$HOME/.local/share/longhouse/provider-release-proofs" \
  -czf /tmp/provider-release-proofs.tar.gz .

cat /tmp/provider-release-proofs.tar.gz \
  | ssh clifford "docker exec -i sauron sh -lc '
      set -eu
      ts=\$(date -u +%Y%m%dT%H%M%SZ)
      base=/data/provider-release-proofs
      [ ! -d \"\$base\" ] || mv \"\$base\" \"\$base.backup.\$ts\"
      mkdir -p \"\$base\"
      tar -xzf - -C \"\$base\"
      test \"\$(find \"\$base\" \\( -name \"._*\" -o -name \".DS_Store\" \\) -print | wc -l | tr -d \" \")\" = 0
    '"
```

Verify all accepted scenarios from inside the Sauron container:

```bash
ssh clifford 'docker exec -i sauron sh -lc "cd /data/jobs && PYTHONPATH=. python3 -"' <<'PY'
from jobs.agents.release_envelope import baseline_status

for provider, scenario_id in [
    ('antigravity', 'antigravity-release-proof-v1'),
    ('antigravity', 'antigravity-real-agy-send-release-proof-v1'),
    ('claude', 'claude-release-proof-v1'),
    ('codex', 'codex-release-proof-v1'),
    ('codex', 'codex-managed-live-send-release-proof-v1'),
    ('codex', 'codex-real-tool-release-proof-v1'),
    ('opencode', 'opencode-release-proof-v1'),
    ('opencode', 'opencode-real-tool-release-proof-v1'),
]:
    status = baseline_status(provider=provider, scenario_id=scenario_id)
    assert status['accepted'] is True, status
    assert status['verdict'] == 'green', status
    assert status['missing_archived_artifacts'] == [], status
    print(provider, scenario_id, status['provider_version'], 'green')
PY
```

The status should be `verdict=green`, `accepted=true`, and
`missing_archived_artifacts=[]`.

Longhouse also has a single inventory check that reads
`docs/specs/provider-release-proof-coverage.json` and verifies every scenario
listed in `accepted_release_proof_scenarios`:

```bash
make provider-release-proof-status-all \
  BASELINE_ROOT="$HOME/.local/share/longhouse/provider-release-proofs" \
  ARTIFACT=/tmp/provider-release-proof-status-all.json
```

For production Sauron, the explicit `/data/jobs` verification above remains
the canonical container check. If a Longhouse checkout is available in the
container or on a machine with access to the same baseline store, the equivalent
inventory check is:

```bash
python3 scripts/qa/provider-release-proof-baseline.py status-all \
  --coverage docs/specs/provider-release-proof-coverage.json \
  --baseline-root /data/provider-release-proofs \
  --artifact /tmp/provider-release-proof-status-all.json \
  --json
```

`provider_release_proof_baseline_status_all.verdict=green` means every scenario
the matrix claims as accepted exists, remains green, and still has its archived
artifacts. Any non-green entry is a repair/reaccept task before claiming that
scenario is protected.

Sauron also runs a daily guard, `agent-release-baseline-guard`, against the same
accepted-scenario inventory. The guard auto-clones Longhouse into
`/data/agent-release-baseline-guard/longhouse` when `LONGHOUSE_REPO_PATH` is not
mounted, reads `docs/specs/provider-release-proof-coverage.json`, and checks
`/data/provider-release-proofs`. A configured non-green inventory returns
`status=degraded`, so it appears in automation health and the daily health
digest without retry noise.

Manual guard check from the live container:

```bash
ssh clifford "docker exec sauron sh -lc 'cd /data/jobs && PYTHONPATH=/data/jobs:/app python - <<\"PY\"
import asyncio, json
from jobs.agent_releases.baseline_guard import run
print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
PY'"
```

Expected healthy summary:

```json
{
  "status": "healthy",
  "verdict": "green",
  "scenario_count": 8,
  "green_count": 8,
  "non_green_count": 0,
  "artifact_path": "/data/provider-release-proofs/baseline-status-all.json"
}
```

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
- `agent-release-baseline-guard`: daily Sauron job that checks the promoted
  baseline store itself, independent of new upstream releases.

The Sauron alert path currently treats `baseline_missing` and
`insufficient_coverage` as non-actionable yellows for inbox purposes. Other
yellow failure codes should reach the inbox as warnings.

Codex Runtime Host tokens are read from environment variables and passed to
nested proof commands through `CODEX_AGENTS_TOKEN`; they should not appear in
argv, raw command evidence, or published fallback artifacts.

Claude Runtime Host machine-live proof is an explicit opt-in scenario:

```bash
CLAUDE_AGENTS_TOKEN=... \
scripts/qa/provider-release-proof.py \
  --provider claude \
  --provider-bin /path/to/claude \
  --provider-version "Claude Code 2.1.181" \
  --claude-run-machine-live-proof \
  --claude-api-url https://your-longhouse-runtime \
  --claude-device-id cinder \
  --artifact /tmp/claude-machine-live-proof.json \
  --evidence-root /tmp/claude-machine-live-proof-evidence \
  --json
```

This uses scenario `claude-machine-live-release-proof-v1`, posts to the Runtime
Host `provider-live-proof` operation, polls for completion, and attaches
machine-live `send_input`, `transcript_binding`, and `steer_active_turn`
operation evidence. The agents token is read from `CLAUDE_AGENTS_TOKEN` and
should not appear in the artifact tree.

A machine-live proof is green only when those three operations are present with
`level=manual_live_token`. During Runtime Host rollout, older hosts may reject
the live-token request fields; the wrapper retries without those fields for
compatibility, but the result stays yellow with
`claude_machine_live_insufficient_coverage` if the machine returns only
no-token launch evidence.

Claude also has a simpler local real-print proof:

```bash
CLAUDE_RUN_REAL_PRINT=1 \
CLAUDE_PRINT_TIMEOUT_SECS=90 \
make provider-release-proof \
  PROVIDER=claude \
  PROVIDER_BIN="$(command -v claude)" \
  PROVIDER_VERSION="$(claude --version)" \
  SOURCE_REVIEW_STATUS=pass \
  ARTIFACT=/tmp/claude-real-print-proof.json \
  EVIDENCE_ROOT=/tmp/claude-real-print-proof-evidence
```

This uses scenario `claude-real-print-release-proof-v1` and spends one local
`claude --print --output-format stream-json` turn. Accept it only when the proof
is green and the nested `claude_real_print` canary shows an exact marker result.
On 2026-06-19, local `claude auth status --json` reported logged in, but this
proof returned red with `failure_code=claude_real_print_api_error` for
`2.1.161 (Claude Code)`. That is an actionable local auth/run divergence, not
an accepted baseline.

## Current Provider State

- OpenCode: accepted baseline `opencode-release-proof-v1`, provider version
  `opencode 1.16.2`.
- OpenCode real-tool: accepted baseline `opencode-real-tool-release-proof-v1`,
  provider version `opencode 1.16.2`. The accepted proof showed a real
  `opencode run --format json` completed `bash` tool event with `callID`,
  structured command input, marker output, and
  `operation_evidence.transcript_binding.level=live_token`.
- Claude Code: accepted scoped baseline `claude-release-proof-v1`, provider
  version `claude 2.1.161`.
- Claude Code real-print: no accepted baseline yet for
  `claude-real-print-release-proof-v1`; a local run on 2026-06-19 returned
  `claude_real_print_api_error` even though `claude auth status --json`
  reported logged in.
- Claude Code machine-live: no accepted baseline yet for
  `claude-machine-live-release-proof-v1`; the Longhouse wrapper can run it when
  Runtime Host URL/token/device credentials are supplied. On 2026-06-19,
  production Runtime Host accepted the fallback path but returned only
  `launch_local` no-token evidence, so the proof correctly stayed yellow with
  `claude_machine_live_insufficient_coverage`.
- Antigravity: accepted scoped baseline `antigravity-release-proof-v1`, provider
  version `agy 1.0.8`.
- Antigravity real-agy send: accepted baseline
  `antigravity-real-agy-send-release-proof-v1`, provider version `agy 1.0.10`.
  The accepted proof showed `operation_evidence.send_input.level=live_token`, a
  claimed Longhouse inbox message, no pending inbox files after the run, and a
  model-visible injected marker in stdout.
- Codex/OpenAI: accepted baseline `codex-release-proof-v1`, provider version
  `codex-cli 0.139.0`.
- Codex/OpenAI live-send: accepted baseline
  `codex-managed-live-send-release-proof-v1`, provider version
  `codex-cli 0.139.0`. The accepted proof store is promoted to production
  Sauron and verifies green. Production Sauron has Runtime Host URL/token
  credentials for scheduled Codex live-send release-watch; a no-spend preflight
  in the `sauron` container on 2026-06-19 returned green for
  `codex-managed-live-send-release-proof-v1`.
- Codex/OpenAI live-interrupt: accepted baseline
  `codex-managed-live-interrupt-release-proof-v1`, provider version
  `codex-cli 0.139.0`. The proof showed managed TUI attach, detached-UI launch,
  reattach, and `operation_evidence.interrupt.level=live_token`. Sauron has an
  opt-in proof/diff pass-through with
  `AGENT_RELEASE_CODEX_MANAGED_LIVE_INTERRUPT=1`; production Sauron enables it
  after promoting the accepted baseline.
- Codex/OpenAI real-tool: accepted local baseline
  `codex-real-tool-release-proof-v1`, provider version `codex-cli 0.139.0`.
  The accepted proof showed real `codex exec --json` completed
  `command_execution` events, exact marker output, a DONE `agent_message`, and
  `operation_evidence.run_once/transcript_binding.level=live_token`. This
  baseline is promoted to production Sauron, and Sauron jobs `3a8d4ba` can run
  it in release-watch with `AGENT_RELEASE_CODEX_REAL_TOOL=1`. Production Sauron
  leaves that token-spending lane off by default.
- Sauron baseline inventory guard: live in production as
  `agent-release-baseline-guard`; on 2026-06-19 it returned 9/9 accepted
  scenarios green against `/data/provider-release-proofs`.
- Antigravity real-agy send release-watch: Sauron has an env-gated pass-through
  for this scenario, and production Sauron now has
  `AGENT_RELEASE_ANTIGRAVITY_REAL_AGY_SEND=1` configured.
- OpenCode real-tool release-watch: Sauron has an env-gated pass-through for
  this scenario, and production Sauron now has
  `AGENT_RELEASE_OPENCODE_REAL_TOOL=1` configured.

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
   For schema v2, add the accepted scenario to
   `accepted_release_proof_scenarios` and reference its `scenario_id` from each
   covered row's `baseline_scenarios`; the coverage test rejects unreferenced
   `release_proof` claims.

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
  tests/test_agent_release_baseline_guard.py \
  tests/test_agent_release_envelope.py \
  tests/test_agent_release_provider_status.py \
  -q
```
