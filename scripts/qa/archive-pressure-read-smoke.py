#!/usr/bin/env python3
"""Prove repair cannot force critical archive-backed reads to return 503."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

_USER_AGENT = "LonghouseArchivePressureSmoke/1"


def request(
    base_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, float, str]:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"X-Agents-Token": token, "User-Agent": _USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=70) as response:
            response_body = response.read()
            return response.status, time.monotonic() - started_at, response_body[:500].decode(errors="replace")
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        return exc.code, time.monotonic() - started_at, response_body[:500].decode(errors="replace")


def json_request(base_url: str, token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"X-Agents-Token": token, "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=70) as response:
        return json.load(response)


def set_repair(base_url: str, token: str, device: str, mode: str, lease_seconds: int = 300) -> int:
    status, _elapsed, _body = request(
        base_url,
        token,
        f"/api/agents/machines/{urllib.parse.quote(device)}/archive-backlog/control",
        method="POST",
        payload={
            "mode": mode,
            "include_huge": False,
            "max_tick_bytes": 512 * 1024 * 1024,
            "lease_seconds": lease_seconds,
        },
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--duration-seconds", type=int, default=60)
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    sessions = json_request(args.api_url, token, "/api/agents/sessions?limit=1")
    session_id = sessions["sessions"][0]["id"]
    day = date.today().isoformat()
    probes = {
        "ingest-health": "/api/agents/ingest-health",
        "worklog": f"/api/agents/worklog/day?date={day}&timezone=America%2FDenver",
        "archive-manifest": "/api/agents/sessions/archive-manifest?limit=1&days_back=3650",
        "session-detail": f"/api/agents/sessions/{session_id}",
    }
    failures: list[dict[str, object]] = []
    counts = {name: 0 for name in probes}
    max_latency_seconds = {name: 0.0 for name in probes}

    start_status = set_repair(args.api_url, token, args.device, "drain", max(60, args.duration_seconds + 60))
    if start_status != 200:
        raise SystemExit(f"could not start leased max-safe repair: HTTP {start_status}")
    try:
        deadline = time.monotonic() + args.duration_seconds
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as pool:
            while time.monotonic() < deadline:
                future_names = {
                    pool.submit(request, args.api_url, token, path): name for name, path in probes.items()
                }
                for future, name in future_names.items():
                    status, elapsed, response_body = future.result()
                    counts[name] += 1
                    max_latency_seconds[name] = max(max_latency_seconds[name], elapsed)
                    if status != 200:
                        failures.append(
                            {
                                "probe": name,
                                "status": status,
                                "elapsed_seconds": round(elapsed, 3),
                                "body": response_body,
                            }
                        )
                time.sleep(0.25)
    finally:
        pause_status = set_repair(args.api_url, token, args.device, "paused")
        if pause_status != 200:
            failures.append({"probe": "repair-pause", "status": pause_status})

    result = {
        "counts": counts,
        "failures": failures,
        "max_latency_seconds": {name: round(value, 3) for name, value in max_latency_seconds.items()},
        "session_id": session_id,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
