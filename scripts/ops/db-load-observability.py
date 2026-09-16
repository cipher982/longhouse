#!/usr/bin/env python3
"""Capture and summarize bounded SQLite load evidence on a runtime host.

The sampler is deliberately host-local: it snapshots a runtime's authenticated
safe telemetry schema and cgroup-v2 counters without adding an observability
service or changing application behavior.  It writes append-only NDJSON so the
same evidence can be compared before and after a maintenance operation.

Only the whitelisted catalog writer and resource fields are persisted.  In
particular, the trusted ``/api/health`` body is never written to disk: it can
contain migration logs, environment details, addresses, and error text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
PROMETHEUS_SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)(?:\s+\S+)?$")
PROMETHEUS_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\\\|\\\"|[^"])*)"')
_SAFE_METRIC_NAMES = frozenset(
    {
        "longhouse_sqlite_wal_bytes",
        "longhouse_live_sqlite_wal_bytes",
        "longhouse_write_serializer_queue_depth",
        "longhouse_write_serializer_writer_active",
        "longhouse_write_serializer_queue_wait_ms",
        "longhouse_write_serializer_exec_ms",
        "longhouse_live_write_serializer_queue_depth",
        "longhouse_live_write_serializer_writer_active",
        "longhouse_live_write_serializer_queue_wait_ms",
        "longhouse_live_write_serializer_exec_ms",
    }
)
_SAFE_ADMISSION_FIELDS = (
    "depth",
    "max_depth",
    "peak_depth",
    "active_label",
    "active_age_ms",
    "rejected_busy",
    "expired_before_execution",
)
_SAFE_QUANTILES = ("p50", "p95", "p99", "max")


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
            key: bytes(value.replace(r'\"', '"').replace(r"\\", "\\"), "utf-8").decode("unicode_escape")
            for key, value in PROMETHEUS_LABEL.findall(match.group("labels") or "")
        }
        samples.append({"name": match.group("name"), "labels": labels, "value": value_json})
    return samples


def _safe_error(exc: BaseException) -> str:
    """Persist an error class, never command output or trusted health text."""
    return type(exc).__name__


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _safe_quantiles(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        key: number
        for key in _SAFE_QUANTILES
        if (number := _finite_number(value.get(key))) is not None
    }


def _safe_labels(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    labels: dict[str, dict[str, Any]] = {}
    # CatalogWriterStats is bounded, but retain a second cap at the trust
    # boundary so a malformed peer cannot turn the append-only lane unbounded.
    for raw_label, raw_stats in list(value.items())[:256]:
        if (
            not isinstance(raw_label, str)
            or not raw_label
            or len(raw_label) > 128
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", raw_label) is None
            or not isinstance(raw_stats, dict)
        ):
            continue
        stats: dict[str, Any] = {}
        count = raw_stats.get("n")
        if type(count) is int and count >= 0:
            stats["n"] = count
        total = _finite_number(raw_stats.get("total_exec_ms"))
        if total is not None and total >= 0:
            stats["total_exec_ms"] = total
        queue_wait = _safe_quantiles(raw_stats.get("queue_wait_ms"))
        exec_time = _safe_quantiles(raw_stats.get("exec_ms"))
        if queue_wait:
            stats["queue_wait_ms"] = queue_wait
        if exec_time:
            stats["exec_ms"] = exec_time
        if stats:
            labels[raw_label] = stats
    return labels


def _safe_admission(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in _SAFE_ADMISSION_FIELDS:
        item = value.get(field)
        if field == "active_label":
            if item is None or isinstance(item, str):
                result[field] = item
        elif field in {"depth", "max_depth", "peak_depth", "rejected_busy", "expired_before_execution"}:
            if type(item) is int and item >= 0:
                result[field] = item
        else:
            number = _finite_number(item)
            if number is not None and number >= 0:
                result[field] = number
    labels = _safe_labels(value.get("labels"))
    if labels:
        result["labels"] = labels
    return result


def _safe_wal(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in ("wal_bytes", "live_wal_bytes"):
        number = _finite_number(value.get(field))
        if number is not None and number >= 0:
            result[field] = number
    checkpoints = value.get("checkpoints")
    if isinstance(checkpoints, dict):
        safe_checkpoints = {}
        for field in ("busy", "log_frames", "checkpointed_frames"):
            item = checkpoints.get(field)
            if type(item) is int and item >= 0:
                safe_checkpoints[field] = item
        if safe_checkpoints:
            result["checkpoints"] = safe_checkpoints
    return result


def _safe_catalogd_health(health: object) -> dict[str, Any]:
    """Project trusted health into the small catalogd schema we retain."""
    if not isinstance(health, dict):
        return {}
    checks = health.get("checks")
    if not isinstance(checks, dict):
        return {}
    catalogd = checks.get("catalogd")
    if not isinstance(catalogd, dict):
        return {}
    result: dict[str, Any] = {}
    for field in ("schema_version", "pid"):
        item = catalogd.get(field)
        if field == "schema_version":
            if type(item) is int and item >= 0:
                result[field] = item
        elif type(item) is int and item > 0:
            result[field] = item
    for field in ("schema_generation", "commit_seq"):
        item = catalogd.get(field)
        if isinstance(item, str) and len(item) <= 256:
            result[field] = item
    admission = _safe_admission(catalogd.get("writer_admission"))
    if admission:
        result["writer_admission"] = admission
    wal = _safe_wal(checks.get("sqlite_wal"))
    if wal:
        result["sqlite_wal"] = wal
    return result


def _safe_metrics(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sample in samples:
        if sample.get("name") not in _SAFE_METRIC_NAMES:
            continue
        value = sample.get("value")
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            continue
        labels = sample.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        safe_labels = {
            key: label
            for key, label in labels.items()
            if key in {"label", "quantile"} and isinstance(label, str) and len(label) <= 128
        }
        result.append({"name": sample["name"], "labels": safe_labels, "value": value})
    return result


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
    errors: dict[str, str] = {}
    try:
        payload["container"] = docker_inspect(args.runtime_container)
    except Exception as exc:
        errors["container"] = _safe_error(exc)
    try:
        payload["metrics"] = _safe_metrics(parse_prometheus(docker_curl(args.runtime_container, "/metrics", "X-Internal-Token")))
    except Exception as exc:
        errors["metrics"] = _safe_error(exc)
    try:
        health = json.loads(docker_curl(args.runtime_container, "/api/health", "X-Internal-Token"))
        catalogd = _safe_catalogd_health(health)
        if catalogd:
            payload["catalogd"] = catalogd
        else:
            errors["health"] = "missing_safe_catalogd"
    except Exception as exc:
        errors["health"] = _safe_error(exc)

    container = payload.get("container")
    catalogd = payload.get("catalogd") or {}
    if isinstance(container, dict):
        incarnation = {
            "container_id": container.get("id") or None,
            "container_started_at": container.get("started_at") or None,
            "process_id": (catalogd.get("pid") if isinstance(catalogd, dict) else None),
        }
        # The process id is retained as a numeric incarnation discriminator;
        # no command output or trusted health detail is retained.
        if incarnation["process_id"] is None:
            incarnation["process_id"] = container.get("pid") or None
        if any(value is not None for value in incarnation.values()):
            payload["incarnation"] = incarnation
    if errors:
        payload["status"] = "partial" if any(key in payload for key in ("metrics", "catalogd")) else "error"
        payload["errors"] = errors
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
            errors.append(f"{container}: {_safe_error(exc)}")
    try:
        payload["mount"] = json.loads(run(["findmnt", "--json", "-T", args.mountpoint, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]))
    except Exception as exc:
        errors.append(f"findmnt: {_safe_error(exc)}")
    try:
        # JSON avoids locale-dependent column parsing.  One report is enough:
        # cgroup counters provide precise per-container deltas between samples.
        payload["iostat"] = json.loads(run(["iostat", "-o", "JSON", "-dx", "1", "1"], timeout=10.0))
    except Exception as exc:
        errors.append(f"iostat: {_safe_error(exc)}")
    if errors:
        payload["status"] = "partial" if payload["containers"] else "error"
        payload["errors"] = errors
    append_ndjson(args.data_dir / "resources.ndjson", payload)
    return 0 if payload["status"] == "ok" else 1


def _lane_paths(path: Path) -> list[Path]:
    lanes = [path] if path.exists() else []
    rotation_name = re.compile(re.escape(path.name) + r"\.\d+(?:\.gz)?")
    lanes.extend(sorted(candidate for candidate in path.parent.glob(path.name + ".*") if candidate.is_file() and rotation_name.fullmatch(candidate.name)))
    if path.suffix == ".gz" and path not in lanes:
        lanes.append(path)
    return lanes


def _read_lane(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{number}: invalid NDJSON: {exc}") from exc
            if isinstance(row, dict):
                yield row


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def _interval_time(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC) if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return _parse_time(str(value))


def read_rows(path: Path, *, start: object = None, end: object = None) -> list[dict[str, Any]]:
    """Read active and rotated gzip lanes once, optionally inclusively bounded."""
    lower = _interval_time(start)
    upper = _interval_time(end)
    if start is not None and lower is None:
        raise ValueError("start must be an ISO-8601 timestamp")
    if end is not None and upper is None:
        raise ValueError("end must be an ISO-8601 timestamp")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("start must not be after end")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lane in _lane_paths(path):
        for row in _read_lane(lane):
            observed = _parse_time(row.get("observed_at"))
            if lower is not None and (observed is None or observed < lower):
                continue
            if upper is not None and (observed is None or observed > upper):
                continue
            # Hash only selected observations: an interval query must not retain
            # the complete trusted-health corpus merely to deduplicate it.
            identity = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    rows.sort(key=lambda row: (row.get("observed_at") or "", json.dumps(row, sort_keys=True, separators=(",", ":"))))
    return rows


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[round((len(values) - 1) * q)]


def _writer_observation(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return (incarnation, safe writer snapshot), including old retained rows."""
    catalogd = row.get("catalogd")
    if isinstance(catalogd, dict):
        admission = catalogd.get("writer_admission")
        labels = admission.get("labels") if isinstance(admission, dict) else None
        if isinstance(labels, dict):
            incarnation = _incarnation_key(row, catalogd)
            return incarnation, {"labels": labels, "sqlite_wal": catalogd.get("sqlite_wal") or {}}

    # Backward analysis is intentionally limited to the old safe serializer
    # fields already retained on disk. New samples never write this shape.
    health = row.get("health")
    checks = health.get("checks") if isinstance(health, dict) else None
    serializer = checks.get("write_serializer") if isinstance(checks, dict) else None
    if not isinstance(serializer, dict):
        return None
    raw_counts = serializer.get("label_counts")
    labels: dict[str, dict[str, Any]] = {}
    if isinstance(raw_counts, dict):
        for label, count in raw_counts.items():
            if isinstance(label, str) and isinstance(count, (int, float)) and not isinstance(count, bool):
                labels[label] = {"n": count}
    rolling = serializer.get("rolling_by_label")
    if isinstance(rolling, dict):
        for label, timing in rolling.items():
            if not isinstance(label, str) or not isinstance(timing, dict):
                continue
            stats = labels.setdefault(label, {})
            for axis in ("queue_wait_ms", "exec_ms"):
                quantiles = timing.get(axis)
                if isinstance(quantiles, dict):
                    stats[axis] = {key: float(value) for key, value in quantiles.items() if key in _SAFE_QUANTILES and _finite_number(value) is not None}
    if not labels:
        return None
    return _incarnation_key(row, {}), {"labels": labels, "sqlite_wal": (checks.get("sqlite_wal") if isinstance(checks, dict) else {}) or {}}


def _incarnation_key(row: dict[str, Any], catalogd: dict[str, Any]) -> str:
    incarnation = row.get("incarnation")
    if isinstance(incarnation, dict):
        fields = {key: incarnation.get(key) for key in ("container_id", "container_started_at", "process_id")}
        if any(value not in (None, "") for value in fields.values()):
            return json.dumps(fields, sort_keys=True, separators=(",", ":"))
    container = row.get("container")
    if isinstance(container, dict):
        fields = {
            "container_id": container.get("id") or None,
            "container_started_at": container.get("started_at") or None,
            "process_id": catalogd.get("pid") or container.get("pid") or None,
        }
        if any(value not in (None, "") for value in fields.values()):
            return json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "legacy"


def _counter_deltas(observations: list[tuple[str, dict[str, Any]]], field: str) -> tuple[Counter[str], int, dict[str, Counter[str]]]:
    previous: dict[str, dict[str, float]] = {}
    totals: Counter[str] = Counter()
    by_incarnation: dict[str, Counter[str]] = defaultdict(Counter)
    resets = 0
    for incarnation, snapshot in observations:
        current: dict[str, float] = {}
        for label, stats in (snapshot.get("labels") or {}).items():
            value = _finite_number(stats.get(field)) if isinstance(stats, dict) else None
            if value is None or value < 0:
                continue
            current[str(label)] = value
        before = previous.get(incarnation)
        if before is not None:
            for label, value in current.items():
                old = before.get(label)
                if old is None:
                    continue
                delta = value - old
                if delta < 0:
                    resets += 1
                    continue
                totals[label] += delta
                by_incarnation[incarnation][label] += delta
        previous[incarnation] = current
    return totals, resets, by_incarnation


def _quantile_report(observations: list[tuple[str, dict[str, Any]]], axis: str) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, list[float]]], dict[str, dict[str, dict[str, float | None]]]]:
    series: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_incarnation: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for incarnation, snapshot in observations:
        for label, stats in (snapshot.get("labels") or {}).items():
            values = stats.get(axis) if isinstance(stats, dict) else None
            if not isinstance(values, dict):
                continue
            for quantile in _SAFE_QUANTILES:
                number = _finite_number(values.get(quantile))
                if number is not None and number >= 0:
                    series[str(label)][quantile].append(number)
                    by_incarnation[incarnation][str(label)][quantile].append(number)

    # A direct quantile value is the latest value in that same sampled series;
    # it is never computed by taking a percentile over p50/p95/p99 values.
    latest = {label: {q: (values[-1] if values else None) for q, values in quantiles.items()} for label, quantiles in series.items()}
    raw_series = {label: dict(quantiles) for label, quantiles in series.items()}
    per_incarnation = {
        incarnation: {
            label: {q: (values[-1] if values else None) for q, values in quantiles.items()}
            for label, quantiles in labels.items()
        }
        for incarnation, labels in by_incarnation.items()
    }
    return latest, raw_series, per_incarnation


def serializer_summary(runtime_rows: list[dict[str, Any]], *, start: object = None, end: object = None) -> dict[str, Any]:
    lower = _interval_time(start)
    upper = _interval_time(end)
    if start is not None and lower is None:
        raise ValueError("start must be an ISO-8601 timestamp")
    if end is not None and upper is None:
        raise ValueError("end must be an ISO-8601 timestamp")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("start must not be after end")

    selected: list[dict[str, Any]] = []
    unparseable_timestamps = 0
    for row in runtime_rows:
        observed = _parse_time(row.get("observed_at"))
        if observed is None:
            unparseable_timestamps += 1
            if lower is not None or upper is not None:
                continue
        elif lower is not None and observed < lower:
            continue
        elif upper is not None and observed > upper:
            continue
        selected.append(row)
    selected.sort(
        key=lambda item: (
            _parse_time(item.get("observed_at")) is None,
            _parse_time(item.get("observed_at")) or dt.datetime.min.replace(tzinfo=dt.UTC),
        )
    )

    observations: list[tuple[str, dict[str, Any]]] = []
    partial_samples = 0
    error_samples = 0
    missing_writer_samples = 0
    wal: list[float] = []
    checkpoints: list[dict[str, Any]] = []
    for row in selected:
        status = row.get("status")
        if status == "partial":
            partial_samples += 1
        elif status == "error":
            error_samples += 1
        observation = _writer_observation(row)
        if observation is None:
            missing_writer_samples += 1
            continue
        observations.append(observation)
        sqlite_wal = observation[1].get("sqlite_wal") or {}
        wal_bytes = _finite_number(sqlite_wal.get("wal_bytes")) if isinstance(sqlite_wal, dict) else None
        if wal_bytes is not None:
            wal.append(wal_bytes)
        if isinstance(sqlite_wal, dict) and isinstance(sqlite_wal.get("checkpoints"), dict):
            checkpoints.append(sqlite_wal["checkpoints"])

    counts, count_resets, count_by_incarnation = _counter_deltas(observations, "n")
    durations, duration_resets, duration_by_incarnation = _counter_deltas(observations, "total_exec_ms")
    queue_wait, queue_series, queue_by_incarnation = _quantile_report(observations, "queue_wait_ms")
    exec_time, exec_series, exec_by_incarnation = _quantile_report(observations, "exec_ms")

    incarnations = sorted({incarnation for incarnation, _snapshot in observations})
    by_incarnation: dict[str, Any] = {}
    for incarnation in incarnations:
        by_incarnation[incarnation] = {
            "write_count_delta_by_label": dict(sorted(count_by_incarnation.get(incarnation, {}).items())),
            "exec_total_ms_delta_by_label": dict(sorted(duration_by_incarnation.get(incarnation, {}).items())),
            "queue_wait_ms": queue_by_incarnation.get(incarnation, {}),
            "exec_ms": exec_by_incarnation.get(incarnation, {}),
        }

    return {
        "write_count_delta_by_label": dict(counts.most_common()),
        "exec_total_ms_delta_by_label": dict(durations.most_common()),
        "queue_wait_ms": queue_wait,
        "exec_ms": exec_time,
        "queue_wait_ms_series": queue_series,
        "exec_ms_series": exec_series,
        "by_incarnation": by_incarnation,
        "wal_bytes": {"min": min(wal) if wal else None, "max": max(wal) if wal else None},
        "checkpoint_samples": len(checkpoints),
        "data_quality": {
            "rows_considered": len(selected),
            "writer_observations": len(observations),
            "partial_samples": partial_samples,
            "error_samples": error_samples,
            "missing_writer_samples": missing_writer_samples,
            "counter_resets": count_resets + duration_resets,
            "count_counter_resets": count_resets,
            "duration_counter_resets": duration_resets,
            "incarnations": incarnations,
            "unparseable_timestamps": unparseable_timestamps,
            "interval_start": lower.isoformat().replace("+00:00", "Z") if lower else None,
            "interval_end": upper.isoformat().replace("+00:00", "Z") if upper else None,
        },
    }


def _resource_identity(container: dict[str, Any]) -> str:
    fields = {
        "container_id": container.get("id") or None,
        "container_started_at": container.get("started_at") or None,
        "process_id": container.get("pid") or None,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":")) if any(value is not None for value in fields.values()) else "legacy"


def resource_summary(resource_rows: list[dict[str, Any]]) -> dict[str, Any]:
    previous: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    totals: Counter[str] = Counter()
    resets = 0
    partial_samples = sum(row.get("status") == "partial" for row in resource_rows)
    error_samples = sum(row.get("status") == "error" for row in resource_rows)
    for row in resource_rows:
        timestamp = str(row.get("observed_at") or "")
        for container in row.get("containers") or []:
            if not isinstance(container, dict):
                continue
            identity = _resource_identity(container)
            cpu = container.get("cpu_stat") or {}
            io = container.get("io_stat") or {}
            if identity in previous:
                _, old_cpu, old_io = previous[identity]
                current_cpu = int(cpu.get("usage_usec") or 0)
                old_usage = int(old_cpu.get("usage_usec") or 0)
                if current_cpu >= old_usage:
                    totals[f"{container.get('name')}:cpu_usec"] += current_cpu - old_usage
                else:
                    resets += 1
                for device, counters in io.items():
                    before = old_io.get(device) or {}
                    for key in ("rbytes", "wbytes", "rios", "wios"):
                        current = int(counters.get(key) or 0)
                        old = int(before.get(key) or 0)
                        if current >= old:
                            totals[f"{container.get('name')}:{key}"] += current - old
                        else:
                            resets += 1
            previous[identity] = (timestamp, cpu, io)
    return {
        "container_deltas": dict(totals.most_common()),
        "data_quality": {
            "rows_considered": len(resource_rows),
            "partial_samples": partial_samples,
            "error_samples": error_samples,
            "counter_resets": resets,
            "incarnations": sorted(previous),
        },
    }


def analyze(args: argparse.Namespace) -> int:
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    runtime_rows = read_rows(args.data_dir / "runtime.ndjson", start=start, end=end)
    resource_rows = read_rows(args.data_dir / "resources.ndjson", start=start, end=end)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "runtime_samples": len(runtime_rows),
        "resource_samples": len(resource_rows),
        "runtime_errors": sum(row.get("status") != "ok" for row in runtime_rows),
        "resource_errors": sum(row.get("status") != "ok" for row in resource_rows),
        "interval": {"start": start, "end": end, "explicit": start is not None or end is not None},
        "serializer": serializer_summary(runtime_rows, start=start, end=end),
        "resources": resource_summary(resource_rows),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def sample(args: argparse.Namespace) -> int:
    """Capture both lanes without letting a degraded runtime block host evidence."""
    runtime_args = argparse.Namespace(data_dir=args.data_dir, runtime_container=args.runtime_container)
    resource_args = argparse.Namespace(
        data_dir=args.data_dir,
        container=[],
        containers=args.containers,
        mountpoint=args.mountpoint,
    )
    runtime_sample(runtime_args)
    resource_sample(resource_args)
    # Individual records carry the truthful status. A timer failure would hide
    # subsequent host samples and turn one slow endpoint into a data gap.
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--data-dir", type=Path, default=Path("/var/lib/longhouse-db-observability"))
    commands = result.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser("runtime", help="capture authenticated safe runtime telemetry")
    runtime.add_argument("--runtime-container", required=True)
    runtime.set_defaults(handler=runtime_sample)
    resources = commands.add_parser("resources", help="capture cgroup-v2 and disk counters")
    resources.add_argument("--container", action="append", default=[])
    resources.add_argument("--containers", help="comma-separated container names; suitable for an EnvironmentFile")
    resources.add_argument("--mountpoint", default="/data")
    resources.set_defaults(handler=resource_sample)
    analysis = commands.add_parser("analyze", help="summarize retained active and rotated lanes")
    analysis.add_argument("--start", help="inclusive ISO-8601 interval start")
    analysis.add_argument("--end", help="inclusive ISO-8601 interval end")
    analysis.set_defaults(handler=analyze)
    capture = commands.add_parser("sample", help="capture runtime and host lanes independently")
    capture.add_argument("--runtime-container", required=True)
    capture.add_argument("--containers", required=True, help="comma-separated container names")
    capture.add_argument("--mountpoint", default="/data")
    capture.set_defaults(handler=sample)
    return result


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
