#!/usr/bin/env python3
"""Measure the hosted write path, and refuse to report a tail from one run.

Two 180-second probes of provably faster code once returned write p99 396ms and
772ms, because ambient writer load differed between them. Several latency claims
made during that work rested on a single run and were not reliable.

So this tool will not print a tail percentile it cannot defend. It runs the probe
N times, reports the spread across runs, and marks the result INCONCLUSIVE when
the runs disagree more than the effect anyone is likely to be looking for. It
also captures what else held the writer during each run, because "it got slower"
and "something else was running" are the same observation until you separate them.

Usage, from the repo root:

    scripts/ops/write-path-probe.py --tenant david010 --runs 3 --seconds 120

Requires SSH access to the runtime host and a device token at
~/.longhouse/machine/device-token.
"""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
from pathlib import Path

PROBE = r'''
import http.client, json, os, time
tok = os.environ["TOK"]
dur = float(os.environ["DUR"])
def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q / 100))]
def probe(method, path, body, headers, seconds, gap=0.2):
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120); c.connect()
    t0 = time.perf_counter(); lat = []
    while time.perf_counter() - t0 < seconds:
        s = time.perf_counter()
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse(); r.read()
        lat.append((time.perf_counter() - s) * 1000)
        time.sleep(gap)
    c.close()
    return {"n": len(lat), "p50": pct(lat, 50), "p90": pct(lat, 90),
            "p95": pct(lat, 95), "p99": pct(lat, 99), "max": max(lat) if lat else 0.0}
hw = {"X-Agents-Token": tok, "Content-Type": "application/json"}
print(json.dumps({
    "write": probe("POST", "/api/agents/heartbeat", json.dumps({}), hw, dur),
    "floor": probe("GET", "/api/livez", None, {}, 15),
    "read":  probe("GET", "/api/agents/machines", None, {"X-Agents-Token": tok}, 15),
}))
'''


def ssh(host: str, command: str) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ssh failed ({result.returncode}): {result.stderr.strip()[:400]}")
    return result.stdout


def ambient_writers(host: str, container: str) -> list[str]:
    """What else held the single writer during the run.

    Without this, a slower run is indistinguishable from a busier one, which is
    exactly the mistake this tool exists to stop.
    """

    out = ssh(
        host,
        f"docker logs {shlex.quote(container)} --tail 400 2>&1 | grep -oE 'held for [0-9]+ms by [a-z_]+' || true",
    )
    seen: dict[str, int] = {}
    for line in out.splitlines():
        label = line.rsplit(" ", 1)[-1]
        seen[label] = seen.get(label, 0) + 1
    return [f"{label} x{count}" for label, count in sorted(seen.items(), key=lambda kv: -kv[1])]


def run_once(host: str, container: str, token: str, seconds: float) -> dict:
    # -i so the probe body reaches the container over ssh stdin.
    script = f"docker exec -i -e TOK={shlex.quote(token)} -e DUR={seconds} {shlex.quote(container)} python3 -"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, script],
        input=PROBE,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"probe failed: {result.stderr.strip()[:400]}")
    lines = result.stdout.strip().splitlines()
    if not lines:
        # An empty result is a broken probe, not a fast one. Say so rather than
        # letting an IndexError stand in for a diagnosis.
        raise RuntimeError(f"probe produced no output; stderr: {result.stderr.strip()[:400] or '(empty)'}")
    return json.loads(lines[-1])


def spread(values: list[float]) -> float:
    """Relative spread across runs. 0.0 means every run agreed exactly."""

    if len(values) < 2 or max(values) == 0:
        return 0.0
    return (max(values) - min(values)) / max(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="david010")
    parser.add_argument("--host", default="zerg")
    parser.add_argument("--runs", type=int, default=3, help="minimum 3; fewer cannot show a spread")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--token-file", default=str(Path.home() / ".longhouse/machine/device-token"))
    parser.add_argument(
        "--max-spread",
        type=float,
        default=0.35,
        help="relative p99 spread across runs above which the tail is reported as inconclusive",
    )
    args = parser.parse_args()

    if args.runs < 3:
        print("--runs must be at least 3: a tail claim needs a spread, and two points cannot show one.", file=sys.stderr)
        return 2

    token = Path(args.token_file).read_text().strip()
    container = f"longhouse-{args.tenant}"

    runs = []
    for index in range(args.runs):
        print(f"run {index + 1}/{args.runs} ({args.seconds:.0f}s)...", file=sys.stderr)
        result = run_once(args.host, container, token, args.seconds)
        result["ambient"] = ambient_writers(args.host, container)
        runs.append(result)

    print()
    print(f"{'probe':6s} {'metric':7s} " + " ".join(f"{'run' + str(i + 1):>9s}" for i in range(len(runs))) + f" {'median':>9s} {'spread':>8s}")
    print("-" * (14 + 10 * len(runs) + 19))
    verdicts: dict[str, bool] = {}
    for probe_name in ("write", "read", "floor"):
        for metric in ("p50", "p90", "p95", "p99"):
            values = [r[probe_name][metric] for r in runs]
            rel = spread(values)
            cells = " ".join(f"{v:9.1f}" for v in values)
            print(f"{probe_name:6s} {metric:7s} {cells} {statistics.median(values):9.1f} {rel * 100:7.0f}%")
            if metric in ("p95", "p99"):
                verdicts[f"{probe_name}.{metric}"] = rel <= args.max_spread
        print()

    print("ambient writer activity per run (who else held the writer):")
    for index, r in enumerate(runs):
        print(f"  run {index + 1}: {', '.join(r['ambient']) or 'none recorded'}")
    print()

    inconclusive = [name for name, ok in verdicts.items() if not ok]
    if inconclusive:
        print("INCONCLUSIVE for: " + ", ".join(sorted(inconclusive)))
        print(f"Runs disagree by more than {args.max_spread * 100:.0f}%. Do not quote these as a result.")
        print("Compare deterministic stage timings instead, or re-run when the writer is quieter.")
        return 1

    print("Tail is reproducible across runs; the medians above are quotable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
