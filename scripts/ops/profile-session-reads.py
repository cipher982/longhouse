#!/usr/bin/env python3
"""Profile a hosted tenant's session read endpoints: latency, Server-Timing spans, failures.

    scripts/ops/profile-session-reads.py --label before --out before.json \
        --session <session-id> [--session <session-id> ...]
    scripts/ops/profile-session-reads.py --compare before.json after.json

Read-only. Authenticates with this machine's device token
(~/.longhouse/machine/device-token) against the tenant in
~/.longhouse/machine/target-url unless --base-url is given.

Two phases:
- sequential: N reads of each endpoint per session, one at a time;
- burst: what an open tab fires on one update (workspace, projection,
  subagents, session list), two tabs' worth at once, every second, against the
  first session (pick one with an active turn).

Spec: control-plane/docs/specs/catalog-read-path.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request

USER_AGENT = "longhouse-profile-session-reads/1"


def _get(url: str, token: str | None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body, status, timing = (
                response.read(),
                response.status,
                response.headers.get("server-timing", ""),
            )
    except urllib.error.HTTPError as exc:
        body, status, timing = (
            exc.read(),
            exc.code,
            exc.headers.get("server-timing", ""),
        )
    except Exception as exc:  # noqa: BLE001 - a profile records failures, it does not stop on them
        return {
            "status": f"error:{type(exc).__name__}",
            "ms": (time.perf_counter() - started) * 1000,
            "bytes": 0,
            "spans": {},
        }
    spans: dict[str, float] = {}
    for part in timing.split(","):
        bits = part.strip().split(";")
        for bit in bits[1:]:
            if bit.startswith("dur="):
                spans[bits[0]] = float(bit[4:])
    return {
        "status": status,
        "ms": (time.perf_counter() - started) * 1000,
        "bytes": len(body),
        "spans": spans,
    }


def _endpoints(session_id: str) -> dict[str, str]:
    return {
        "workspace": f"/sessions/{session_id}/workspace?limit=200&detail=lite",
        "workspace_header": f"/sessions/{session_id}/workspace?limit=200&detail=lite&include_projection=false",
        "projection": f"/sessions/{session_id}/projection?limit=200&detail=lite",
        "subagents": f"/sessions/{session_id}/subagents",
        "session_list": "/sessions?limit=50",
    }


def _summary(results: list[dict]) -> dict:
    ok = sorted(r["ms"] for r in results if r["status"] == 200)
    out: dict = {
        "n": len(results),
        "ok": len(ok),
        "failures": sorted({str(r["status"]) for r in results if r["status"] != 200}),
    }
    if ok:
        out["p50"] = round(statistics.median(ok), 1)
        out["p95"] = round(ok[min(len(ok) - 1, int(len(ok) * 0.95))], 1)
        out["max"] = round(ok[-1], 1)
    spans: dict[str, list[float]] = {}
    for result in results:
        for name, value in result["spans"].items():
            spans.setdefault(name, []).append(value)
    out["span_p50"] = {
        name: round(statistics.median(values), 1) for name, values in spans.items()
    }
    out["bytes_p50"] = (
        int(statistics.median([r["bytes"] for r in results])) if results else 0
    )
    return out


def profile(
    base_url: str,
    token: str | None,
    sessions: list[str],
    label: str,
    n: int,
    waves: int,
) -> dict:
    api = base_url.rstrip("/") + "/api/timeline"
    with urllib.request.urlopen(
        urllib.request.Request(
            base_url.rstrip("/") + "/api/health", headers={"User-Agent": USER_AGENT}
        ),
        timeout=10,
    ) as response:
        commit = json.loads(response.read()).get("build", {}).get("commit")
    result: dict = {
        "label": label,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": commit,
        "sequential": {},
        "burst": {},
    }
    for index, session_id in enumerate(sessions):
        for name, path in _endpoints(session_id).items():
            result["sequential"][f"s{index}/{name}"] = _summary(
                [_get(api + path, token) for _ in range(n)]
            )
    endpoints = _endpoints(sessions[0])
    wave = ["workspace", "projection", "subagents", "session_list"]
    burst: dict[str, list[dict]] = {name: [] for name in wave}
    with cf.ThreadPoolExecutor(max_workers=len(wave) * 2) as pool:
        for _ in range(waves):
            futures = {
                pool.submit(_get, api + endpoints[name], token): name
                for name in wave * 2
            }
            for future in cf.as_completed(futures):
                burst[futures[future]].append(future.result())
            time.sleep(1.0)
    result["burst"] = {name: _summary(results) for name, results in burst.items()}
    return result


def _print(result: dict) -> None:
    print(f"{result['label']} at {result['at']} commit {result['commit']}")
    for section in ("sequential", "burst"):
        for name, row in result[section].items():
            failures = f" failures={row['failures']}" if row["failures"] else ""
            print(
                f"  {section:10} {name:22} p50={row.get('p50')} p95={row.get('p95')} ok={row['ok']}/{row['n']}"
                f" bytes={row['bytes_p50']}{failures} spans={row['span_p50']}"
            )


def _compare(before: dict, after: dict) -> None:
    print(
        f"before {before['commit']} ({before['at']})  after {after['commit']} ({after['at']})"
    )
    for section in ("sequential", "burst"):
        for name in before[section]:
            a, b = before[section][name], after[section].get(name)
            if b is None:
                continue
            print(
                f"  {section:10} {name:22} p50 {a.get('p50')} -> {b.get('p50')}  p95 {a.get('p95')} -> {b.get('p95')}"
                f"  failures {a['n'] - a['ok']} -> {b['n'] - b['ok']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="session id; the first gets the burst phase",
    )
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--base-url")
    parser.add_argument(
        "-n", type=int, default=15, help="sequential reads per endpoint per session"
    )
    parser.add_argument(
        "--waves", type=int, default=10, help="burst waves, one per second"
    )
    parser.add_argument(
        "--compare", nargs=2, type=pathlib.Path, metavar=("BEFORE", "AFTER")
    )
    args = parser.parse_args()

    if args.compare:
        _compare(
            json.loads(args.compare[0].read_text()),
            json.loads(args.compare[1].read_text()),
        )
        return 0
    if not args.session:
        parser.error("--session is required")
    machine = pathlib.Path("~/.longhouse/machine").expanduser()
    base_url = args.base_url or (machine / "target-url").read_text().strip()
    token_path = machine / "device-token"
    token = token_path.read_text().strip() if token_path.exists() else None
    result = profile(base_url, token, args.session, args.label, args.n, args.waves)
    _print(result)
    if args.out:
        args.out.write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
