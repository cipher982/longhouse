#!/usr/bin/env python3
"""Capture and summarize lightweight SQLite load evidence on a runtime host.

The sampler is deliberately host-local: it snapshots a runtime's authenticated
metrics/health payloads and cgroup-v2 counters without adding an observability
service or changing application behavior.  It writes append-only NDJSON so the
same evidence can be compared before and after a maintenance operation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PROMETHEUS_SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)(?:\s+\S+)?$")
PROMETHEUS_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\\\|\\\"|[^"])*)"')


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(command: list[str], *, input_text: str | None = None, timeout: float = 30.0) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{' '.join(command[:3])}: {detail}")
    return completed.stdout


def append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_prometheus(text: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_SAMPLE.match(line)
        if not match:
            continue
        raw_value = match.group("value")
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            value_json: float | str = raw_value
        else:
            value_json = value
        labels = {
            key: bytes(value.replace(r'\\"', '"').replace(r"\\\\", "\\"), "utf-8").decode("unicode_escape")
            for key, value in PROMETHEUS_LABEL.findall(match.group("labels") or "")
        }
        samples.append({"name": match.group("name"), "labels": labels, "value": value_json})
    return samples


def docker_inspect(container: str) -> dict[str, Any]:
    payload = json.loads(run(["docker", "inspect", container]))
    if not payload:
        raise RuntimeError(f"container not found: {container}")
    item = payload[0]
    return {
        "name": str(item.get("Name") or "").removeprefix("/"),
        "id": str(item.get("Id") or ""),
        "image": str((item.get("Config") or {}).get("Image") or ""),
        "pid": int((item.get("State") or {}).get("Pid") or 0),
        "started_at": str((item.get("State") or {}).get("StartedAt") or ""),
    }


def docker_curl(container: str, path: str, header_name: str) -> str:
    # The internal API secret stays in the runtime container.  The host service
    # gets its authority from root-only Docker access and never copies, logs, or
    # exposes that secret in argv.
    script = (
        "token=${INTERNAL_API_SECRET:?INTERNAL_API_SECRET is required}; "
        f"exec curl --fail --silent --show-error --max-time 20 -H '{header_name}: '"
        '"$token" ' + f"http://127.0.0.1:8000{path}"
    )
    return run(["docker", "exec", container, "sh", "-ceu", script], timeout=30.0)


def runtime_sample(args: argparse.Namespace) -> int:
    observed_at = utc_now()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "runtime",
        "observed_at": observed_at,
        "container": {"requested_name": args.runtime_container},
        "status": "ok",
    }
    try:
        payload["container"] = docker_inspect(args.runtime_container)
        metrics = docker_curl(args.runtime_container, "/metrics", "X-Internal-Token")
        health = docker_curl(args.runtime_container, "/api/health", "X-Internal-Token")
        payload["metrics"] = parse_prometheus(metrics)
        payload["health"] = json.loads(health)
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = str(exc)
    append_ndjson(args.data_dir / "runtime.ndjson", payload)
    return 0 if payload["status"] == "ok" else 1


def cgroup_path_for_pid(pid: int) -> Path:
    if pid <= 0:
        raise RuntimeError("container is not running")
    for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return Path("/sys/fs/cgroup") / parts[2].lstrip("/")
    raise RuntimeError(f"no cgroup v2 path for pid {pid}")


def parse_key_value_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(" ")
        if not key:
            continue
        if "=" in line:
            values = {part.partition("=")[0]: int(part.partition("=")[2]) for part in line.split()[1:] if "=" in part}
            result[key] = values
        else:
            try:
                result[key] = int(value)
            except ValueError:
                result[key] = value
    return result


def resource_sample(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "resources",
        "observed_at": utc_now(),
        "status": "ok",
        "containers": [],
        "mount": {},
        "iostat": {},
    }
    errors: list[str] = []
    containers = [*args.container]
    if args.containers:
        containers.extend(name.strip() for name in args.containers.split(",") if name.strip())
    if not containers:
        raise RuntimeError("at least one --container or --containers value is required")
    for container in containers:
        try:
            info = docker_inspect(container)
            cgroup = cgroup_path_for_pid(info["pid"])
            payload["containers"].append(
                {
                    **info,
                    "cgroup": str(cgroup),
                    "io_stat": parse_key_value_file(cgroup / "io.stat"),
                    "cpu_stat": parse_key_value_file(cgroup / "cpu.stat"),
                }
            )
        except Exception as exc:
            errors.append(f"{container}: {exc}")
    try:
        payload["mount"] = json.loads(run(["findmnt", "--json", "-T", args.mountpoint, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]))
    except Exception as exc:
        errors.append(f"findmnt: {exc}")
    try:
        # JSON avoids locale-dependent column parsing.  One report is enough:
        # cgroup counters provide precise per-container deltas between samples.
        payload["iostat"] = json.loads(run(["iostat", "-o", "JSON", "-dx", "1", "1"], timeout=10.0))
    except Exception as exc:
        errors.append(f"iostat: {exc}")
    if errors:
        payload["status"] = "partial" if payload["containers"] else "error"
        payload["errors"] = errors
    append_ndjson(args.data_dir / "resources.ndjson", payload)
    return 0 if payload["status"] == "ok" else 1


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{path}:{number}: invalid NDJSON: {exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[round((len(values) - 1) * q)]


def serializer_summary(runtime_rows: list[dict[str, Any]]) -> dict[str, Any]:
    first: dict[str, int] | None = None
    last: dict[str, int] | None = None
    queue_wait: dict[str, list[float]] = defaultdict(list)
    exec_time: dict[str, list[float]] = defaultdict(list)
    wal: list[float] = []
    checkpoints: list[dict[str, Any]] = []
    for row in runtime_rows:
        if row.get("status") != "ok":
            continue
        checks = ((row.get("health") or {}).get("checks") or {})
        serializer = checks.get("write_serializer") or {}
        labels = serializer.get("label_counts") or {}
        if isinstance(labels, dict):
            counts = {str(label): int(value) for label, value in labels.items() if isinstance(value, (int, float))}
            first = first or counts
            last = counts
        for label, timing in (serializer.get("rolling_by_label") or {}).items():
            for value in ((timing or {}).get("queue_wait_ms") or {}).values():
                if isinstance(value, (int, float)):
                    queue_wait[str(label)].append(float(value))
            for value in ((timing or {}).get("exec_ms") or {}).values():
                if isinstance(value, (int, float)):
                    exec_time[str(label)].append(float(value))
        sqlite_wal = checks.get("sqlite_wal") or {}
        if isinstance(sqlite_wal.get("wal_bytes"), (int, float)):
            wal.append(float(sqlite_wal["wal_bytes"]))
        if isinstance(sqlite_wal.get("checkpoints"), dict):
            checkpoints.append(sqlite_wal["checkpoints"])
    deltas = {
        label: max(0, last.get(label, 0) - (first or {}).get(label, 0))
        for label in (last or {})
    }
    ranked = sorted(deltas.items(), key=lambda item: item[1], reverse=True)
    return {
        "write_count_delta_by_label": dict(ranked),
        "queue_wait_ms": {label: {"p50": percentile(values, .50), "p95": percentile(values, .95), "p99": percentile(values, .99)} for label, values in queue_wait.items()},
        "exec_ms": {label: {"p50": percentile(values, .50), "p95": percentile(values, .95), "p99": percentile(values, .99)} for label, values in exec_time.items()},
        "wal_bytes": {"min": min(wal) if wal else None, "max": max(wal) if wal else None},
        "checkpoint_samples": len(checkpoints),
    }


def resource_summary(resource_rows: list[dict[str, Any]]) -> dict[str, Any]:
    previous: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    totals: Counter[str] = Counter()
    for row in resource_rows:
        timestamp = str(row.get("observed_at") or "")
        for container in row.get("containers") or []:
            identity = str(container.get("id") or "")
            cpu = container.get("cpu_stat") or {}
            io = container.get("io_stat") or {}
            if identity in previous:
                _, old_cpu, old_io = previous[identity]
                current_cpu = int(cpu.get("usage_usec") or 0)
                totals[f"{container.get('name')}:cpu_usec"] += max(0, current_cpu - int(old_cpu.get("usage_usec") or 0))
                for device, counters in io.items():
                    before = old_io.get(device) or {}
                    for key in ("rbytes", "wbytes", "rios", "wios"):
                        totals[f"{container.get('name')}:{key}"] += max(0, int(counters.get(key) or 0) - int(before.get(key) or 0))
            previous[identity] = (timestamp, cpu, io)
    return {"container_deltas": dict(totals.most_common())}


def analyze(args: argparse.Namespace) -> int:
    runtime_rows = read_rows(args.data_dir / "runtime.ndjson")
    resource_rows = read_rows(args.data_dir / "resources.ndjson")
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "runtime_samples": len(runtime_rows),
        "resource_samples": len(resource_rows),
        "runtime_errors": sum(row.get("status") != "ok" for row in runtime_rows),
        "resource_errors": sum(row.get("status") != "ok" for row in resource_rows),
        "serializer": serializer_summary(runtime_rows),
        "resources": resource_summary(resource_rows),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--data-dir", type=Path, default=Path("/var/lib/longhouse-db-observability"))
    commands = result.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser("runtime", help="capture authenticated runtime metrics and health")
    runtime.add_argument("--runtime-container", required=True)
    runtime.set_defaults(handler=runtime_sample)
    resources = commands.add_parser("resources", help="capture cgroup-v2 and disk counters")
    resources.add_argument("--container", action="append", default=[])
    resources.add_argument("--containers", help="comma-separated container names; suitable for an EnvironmentFile")
    resources.add_argument("--mountpoint", default="/data")
    resources.set_defaults(handler=resource_sample)
    analysis = commands.add_parser("analyze", help="summarize retained NDJSON")
    analysis.set_defaults(handler=analyze)
    return result


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
