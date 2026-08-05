# Launch Reliability Dogfood Runbook

Use this runbook to collect the longitudinal dogfood evidence consumed by
`scripts/qa/launch-reliability-measurements.py`. The collector records one
episode per invocation. It does not infer expected truth from the health
snapshot.

## Rules

- Run from a clean qualification checkout at the exact Longhouse commit being
  measured. Keep artifacts outside the checkout.
- Create one private reporter challenge per episode from that checkout before
  collecting. The challenge binds one artifact to the collector revision and
  provides tamper evidence; keep challenge files private and pass them to the
  final report.
- Supply an independent expected health state, producer-freshness state, red
  eligibility, and action. A value copied from the observed snapshot is not
  independent ground truth.
- Use a distinct `--episode-id` for each real fault/recovery episode. Run the
  collector once per episode; do not turn repeated samples of one ongoing
  incident into separate episodes.
- A failed, timed-out, malformed, or schema-incompatible `local-health` call
  produces a diagnostic artifact that the report rejects. Do not edit that
  artifact to make it usable.
- Collect at least three distinct episodes spanning at least one hour. The
  report keeps the longitudinal measures `not_observed` until that condition
  is met.

Create the private challenge before leaving the clean checkout:

```bash
uv run --project server python scripts/qa/launch-reliability-measurements.py \
  --create-dogfood-challenge /tmp/longhouse-dogfood-challenge.json
```

The command refuses a dirty checkout. Do not commit or copy the challenge into
the repository. It expires after 24 hours and cannot be reused for another
episode. If the source commit or collector changes, create new challenges.

The sampled binary must be built and installed from this same clean checkout.
For the local dogfood install, use `make dogfood-refresh`, then verify:

```bash
longhouse build-identity --json
```

The reported facade and engine commits must equal the qualification checkout
SHA and both must report `dirty=false`. This refresh changes the local runtime;
use the qualification machine/workflow designated for the run.

## Collect one episode

The expected fields must come from the fault injection or operator record:

```bash
uv run --project server python scripts/qa/launch_reliability_dogfood.py \
  --challenge /tmp/longhouse-dogfood-challenge-a.json \
  --output /tmp/longhouse-dogfood-episode-a.json \
  --episode-id provider-recovery-a \
  --longhouse-bin /Users/davidrose/.local/bin/longhouse \
  --expected-health-state broken \
  --expected-producer-freshness stale \
  --expected-red-eligible \
  --expected-action inspect_local_health \
  --recovery-duration-seconds 12.4
```

For an event-bearing issue, include its independently recorded lifecycle:

```bash
  --issue-status resolved \
  --issue-opened-at 2026-08-05T12:00:00Z \
  --issue-resolved-at 2026-08-05T12:00:12Z
```

Evidence-conservation counters may be supplied from the owning durable lane:

```bash
  --evidence-conservation-json /tmp/episode-a-conservation.json
```

The counter file must contain non-negative `source_events`, `archived_events`,
`replayed_events`, `duplicate_events`, `discarded_events`, and
`unresolved_events` values. The collector validates the accounting relation;
the report validates it again.

## Produce the report

Pass every retained episode artifact explicitly. Run this command from the
same clean checkout and commit used to collect them:

```bash
uv run --project server python scripts/qa/launch-reliability-measurements.py \
  --matrix-root /path/to/installed-managed-launch-fault-matrix \
  --provider-harness-artifact /path/to/provider-harness.json \
  --health-artifact /path/to/installed-native-health-fault-matrix.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-a.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-b.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-c.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-a.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-b.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-c.json \
  --output /tmp/launch-reliability-measurements.json
```

The report must have `report_status=ok`, `dogfood_series.input_status=valid`,
and an eligible observation window. The report's `qualification.status` remains
`not_qualified` for these operator-held challenge artifacts: they are
diagnostic self-attestation, not an independent release attestation. An
external CI/release receipt is required before quoting the dogfood metrics as
launch evidence. The sampled binary must still exist at the
recorded path, and report facade and engine commits exactly equal to the full
measured source SHA, with `dirty=false`. A stale installed binary therefore
fails closed instead of qualifying an older implementation. Any invalid episode
clears the numerical dogfood claims rather than partially counting the
remaining files.

## Release-owned qualification receipt

The report above is still `qualification.status=not_qualified`. The operator
challenge is an integrity check for diagnosis; it is not the release trust
boundary. A protected qualification runner must derive the subject from the
completed report and sign it with the private half of the committed
`longhouse-ci-2026` Ed25519 key.

The private key must exist only as the protected CI environment secret
`LONGHOUSE_DOGFOOD_ATTESTATION_PRIVATE_KEY_PEM`. Do not put it in the checkout,
an episode artifact, shell history, or a normal developer secret store. The
matching public key is
`config/qa/launch_reliability_attestation_keys.json`; changing it is a trust
root rotation and requires a release review.

Retain the evidence on the protected qualification runner under this fixed
layout before dispatching the `Launch reliability attestation` workflow:

```text
/var/lib/longhouse/qualification/<full-source-sha>/
  matrix/                       # managed-launch matrix directory
  provider-harness/*.json
  native-health/*.json
  dogfood/episodes/*.json
  dogfood/challenges/*.json
```

Dispatch the workflow from protected `main` with the report's full source SHA.
The measurement job runs on the qualification runner, rejects symlinks, checks
that a sorted SHA-256 manifest is unchanged before and after the copy, and
rebuilds the report from the resulting temporary snapshot before the signing
secret is exposed. The separate signer job checks out the workflow commit on
the protected signer runner and receives only the measured report artifact; it
does not read the retained evidence root. It then runs `create-from-report`,
which re-derives and validates the subject before signing, and verifies the
receipt against the committed public key. A missing evidence set, mutable
snapshot, missing secret, dirty subject, source mismatch, unprotected branch,
or key mismatch fails the job. Configure the `longhouse-release-attestation`
environment with required reviewers. The environment approval does not itself
restrict runner selection: use an ephemeral one-job signer runner in a runner
group restricted to this repository and workflow. Confirm the protected-main
branch rules, environment reviewers, runner-group restriction, and
single-job runner behavior before treating a receipt as release evidence.

The workflow supports Linux and macOS X64/ARM64 runners. It pins Python
3.12.13, uv 0.10.10 with a platform-specific checksum, and uses frozen uv
resolution in both jobs.

The qualification runner and its retained evidence are part of the trusted
computing base: the measurement job executes the checked-out source's report
code and reads the finalized evidence tree. Restrict its runner group to this
repository and workflow as well, use a dedicated or ephemeral runner, and
prevent other jobs or users from writing
`/var/lib/longhouse/qualification/<full-source-sha>/` during qualification.
The manifest checks detect ordinary concurrent changes, but they cannot make a
compromised qualification runner trustworthy.

The `report.sha256` file is a transport-corruption check for the uploaded
report. The trusted subject re-derivation and receipt signature are the release
trust boundary.

After the workflow uploads the receipt, regenerate the final report from the
same clean qualification checkout and retained inputs, adding:

```bash
uv run --project server python scripts/qa/launch-reliability-measurements.py \
  --matrix-root /path/to/installed-managed-launch-fault-matrix \
  --provider-harness-artifact /path/to/provider-harness.json \
  --health-artifact /path/to/installed-native-health-fault-matrix.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-a.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-b.json \
  --dogfood-series /tmp/longhouse-dogfood-episode-c.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-a.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-b.json \
  --dogfood-challenge /tmp/longhouse-dogfood-challenge-c.json \
  --dogfood-attestation /path/to/launch-reliability-attestation.json \
  --output /tmp/launch-reliability-qualified.json
```

The final report is qualifying only when it has
`report_status=ok`, `qualification.status=qualified`,
`qualification.dogfood_attestation=external_receipt`, and a receipt subject
hash matching the report's exact inputs. A receipt for a different report,
expired receipt, future-issued receipt, wrong key, or changed artifact fails
closed.
