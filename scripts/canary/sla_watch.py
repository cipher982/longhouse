#!/usr/bin/env python3
"""SLA watch: scrape /metrics, check percentiles, ping on breach.

Runs in a tight loop. Every CHECK_INTERVAL_S:
  1. GET <base>/metrics
  2. Parse canary_latency_seconds and event_end_to_end_latency_seconds buckets
  3. Compute p50/p95/p99 per (surface, managed)
  4. If p95 exceeds SLA_P95_MS for ALERT_CONSECUTIVE samples in a row, POST
     a breach notice to LONGHOUSE_SLA_WEBHOOK

Intentionally simple: no Prometheus, no Alertmanager. A webhook (ntfy /
Discord / Slack) is enough for solo-dev.

Usage:
    LONGHOUSE_CANARY_URL=https://your-instance.longhouse.ai \
    LONGHOUSE_SLA_WEBHOOK='https://ntfy.sh/lh-sla' \
    python3 scripts/canary/sla_watch.py
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from collections import deque
from typing import Iterable

import httpx

CHECK_INTERVAL_S = int(os.environ.get("LONGHOUSE_SLA_CHECK_INTERVAL_S", "60"))
ALERT_CONSECUTIVE = int(os.environ.get("LONGHOUSE_SLA_ALERT_CONSECUTIVE", "3"))
SLA_P95_MS = int(os.environ.get("LONGHOUSE_SLA_P95_MS", "300"))

# Metric we watch. Canary is the leading indicator: if it breaches, users
# will shortly too. event_end_to_end_latency_seconds (user beacons) is
# lagging; we include it for reference but alert on canary first.
WATCH_METRIC = "canary_latency_seconds"
WATCH_HOP = "sse"  # server wake: the hop that most reflects pubsub/SSE health


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"FATAL: missing {key}", file=sys.stderr)
        sys.exit(2)
    return value


def parse_histogram_buckets(metrics_text: str, metric_name: str, label_filter: dict[str, str]) -> list[tuple[float, float]]:
    """Extract (le, cumulative_count) pairs from a Prometheus histogram.

    label_filter matches exact key=value pairs. Buckets with extra labels
    not in the filter are still included as long as the filter subset
    matches; _bucket lines with `le="..."` are the cumulative counts.
    """
    out: list[tuple[float, float]] = []
    bucket_line = re.compile(rf"^{re.escape(metric_name)}_bucket\{{(?P<labels>[^}}]+)\}}\s+(?P<value>[\d.eE+\-]+)$")
    for line in metrics_text.splitlines():
        m = bucket_line.match(line)
        if not m:
            continue
        labels_raw = m.group("labels")
        # Parse the labels as k="v" pairs
        label_pairs = dict(re.findall(r'(\w+)="([^"]*)"', labels_raw))
        # Filter
        if not all(label_pairs.get(k) == v for k, v in label_filter.items()):
            continue
        le_raw = label_pairs.get("le")
        if le_raw is None:
            continue
        le = float("inf") if le_raw == "+Inf" else float(le_raw)
        out.append((le, float(m.group("value"))))
    out.sort(key=lambda pair: pair[0])
    return out


def percentile_from_histogram(buckets: list[tuple[float, float]], p: float) -> float | None:
    """Linear-interpolation-free percentile: return the smallest bucket le
    whose cumulative count >= total * p. Returns None if no data."""
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = total * p
    for le, count in buckets:
        if count >= target:
            return le
    return buckets[-1][0]


def delta_buckets(
    current: list[tuple[float, float]],
    previous: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Subtract previous cumulative counts from current to get per-window counts.

    Prometheus histograms are cumulative from process start; scraping raw
    percentiles makes alerts sticky long after recovery. Windowed alerting
    requires a delta. Counts can go negative on process restart (counters
    reset); clamp to 0 in that case.
    """
    if not current:
        return []
    if not previous or len(previous) != len(current):
        return current
    result: list[tuple[float, float]] = []
    for (le_c, c), (le_p, p) in zip(current, previous):
        if le_c != le_p:
            # Bucket schema changed mid-run; trust current, ignore previous.
            return current
        result.append((le_c, max(0.0, c - p)))
    return result


def post_webhook(webhook: str, title: str, body: str) -> None:
    try:
        # Detect ntfy — it accepts plain text with a Title header.
        if "ntfy" in webhook:
            httpx.post(webhook, headers={"Title": title, "Priority": "high"}, content=body, timeout=10.0)
        else:
            httpx.post(webhook, json={"text": f"{title}\n\n{body}"}, timeout=10.0)
    except Exception as exc:
        print(f"webhook post failed: {exc}", file=sys.stderr)


def main() -> int:
    base_url = _require_env("LONGHOUSE_CANARY_URL").rstrip("/")
    webhook = os.environ.get("LONGHOUSE_SLA_WEBHOOK", "").strip()

    stopping = False

    def _stop(_s, _f):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    breach_history: deque[bool] = deque(maxlen=ALERT_CONSECUTIVE)
    last_alert_at = 0.0
    ALERT_COOLDOWN_S = 600  # 10 min between re-alerts
    previous_buckets: list[tuple[float, float]] = []

    while not stopping:
        try:
            resp = httpx.get(f"{base_url}/metrics", timeout=10.0)
            if resp.status_code != 200:
                print(f"metrics HTTP {resp.status_code}", file=sys.stderr)
                time.sleep(CHECK_INTERVAL_S)
                continue
            cumulative = parse_histogram_buckets(resp.text, WATCH_METRIC, {"hop": WATCH_HOP})
            # Percentile on the delta since last scrape — sliding window that
            # recovers on its own once latency normalizes.
            buckets = delta_buckets(cumulative, previous_buckets)
            previous_buckets = cumulative
            p50 = percentile_from_histogram(buckets, 0.5)
            p95 = percentile_from_histogram(buckets, 0.95)
            p99 = percentile_from_histogram(buckets, 0.99)
            if p95 is None:
                print(f"no samples yet for {WATCH_METRIC}{{hop={WATCH_HOP!r}}}")
            else:
                breach = (p95 * 1000) > SLA_P95_MS
                breach_history.append(breach)
                print(
                    f"{WATCH_METRIC}{{hop={WATCH_HOP}}}: "
                    f"p50={None if p50 is None else round(p50*1000,1)}ms "
                    f"p95={None if p95 is None else round(p95*1000,1)}ms "
                    f"p99={None if p99 is None else round(p99*1000,1)}ms "
                    f"breach={breach}"
                )
                if (
                    len(breach_history) == ALERT_CONSECUTIVE
                    and all(breach_history)
                    and webhook
                    and (time.time() - last_alert_at) > ALERT_COOLDOWN_S
                ):
                    title = f"Longhouse SLA breach: {WATCH_METRIC}[hop={WATCH_HOP}] p95 > {SLA_P95_MS}ms"
                    body = json.dumps({"p50_ms": p50*1000 if p50 else None, "p95_ms": p95*1000, "p99_ms": p99*1000 if p99 else None}, indent=2)
                    post_webhook(webhook, title, body)
                    last_alert_at = time.time()
        except Exception as exc:
            print(f"scrape error: {exc}", file=sys.stderr)

        slept = 0.0
        while slept < CHECK_INTERVAL_S and not stopping:
            time.sleep(min(1.0, CHECK_INTERVAL_S - slept))
            slept += 1.0

    return 0


if __name__ == "__main__":
    sys.exit(main())
