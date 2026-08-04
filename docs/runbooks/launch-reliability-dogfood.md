# Launch Reliability Dogfood Runbook

Use this runbook to collect the longitudinal dogfood evidence consumed by
`scripts/qa/launch-reliability-measurements.py`. The collector records one
episode per invocation. It does not infer expected truth from the health
snapshot.

## Rules

- Run from a clean qualification checkout at the exact Longhouse commit being
  measured. Keep artifacts outside the checkout.
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

## Collect one episode

The expected fields must come from the fault injection or operator record:

```bash
python3 scripts/qa/launch_reliability_dogfood.py \
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
  --output /tmp/launch-reliability-measurements.json
```

The report must have `report_status=ok`, `dogfood_series.input_status=valid`,
and an eligible observation window. Any invalid episode clears the numerical
dogfood claims rather than partially counting the remaining files.
