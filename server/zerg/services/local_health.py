"""Local Longhouse engine health snapshot helpers.

This module is the canonical local-health classifier for the CLI and future
desktop surfaces. It combines raw local probes with a small derived state model
without hiding the underlying signals.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.shipper.service import get_service_info

SCHEMA_VERSION = 1
ENGINE_FRESH_SECONDS = 30
ENGINE_STALE_SECONDS = 120
OUTBOX_DEGRADED_AGE_SECONDS = 15
OUTBOX_BROKEN_AGE_SECONDS = 120
DEGRADED_BACKLOG_COUNT = 1
BROKEN_BACKLOG_COUNT = 25
DISK_DEGRADED_BYTES = 5 * 1024 * 1024 * 1024
DISK_BROKEN_BYTES = 1 * 1024 * 1024 * 1024
ACTIVITY_RECENT_MINUTES = 15
ACTIVITY_RECENCY_BANDS = [
    ("0-1m", timedelta(minutes=1)),
    ("1-5m", timedelta(minutes=5)),
    ("5-15m", timedelta(minutes=15)),
    ("15-60m", timedelta(hours=1)),
    ("1-6h", timedelta(hours=6)),
]
RECENT_TOUCH_LIMIT = 4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_rfc3339(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _coerce_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser()
    return Path.home() / ".claude"


def _read_trimmed_file(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def _read_session_context(path: Path, *, max_lines: int = 6) -> tuple[str | None, str | None]:
    """Extract cwd and branch from the first few JSONL records when available."""
    try:
        with path.open() as handle:
            for index, raw_line in enumerate(handle):
                if index >= max_lines:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cwd = None
                branch = None

                if isinstance(payload, dict):
                    if isinstance(payload.get("payload"), dict):
                        meta = payload["payload"]
                        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
                        if isinstance(meta.get("git"), dict):
                            branch = meta["git"].get("branch") if isinstance(meta["git"].get("branch"), str) else None
                    if isinstance(payload.get("message"), dict):
                        message = payload["message"]
                        cwd = cwd or (message.get("cwd") if isinstance(message.get("cwd"), str) else None)
                        branch = branch or (message.get("gitBranch") if isinstance(message.get("gitBranch"), str) else None)

                if cwd or branch:
                    return cwd, branch
    except OSError:
        return None, None

    return None, None


def _derive_workspace_label(source_path: Path, *, cwd: str | None) -> str | None:
    if cwd:
        name = Path(cwd).name.strip()
        if name:
            return name

    parts = source_path.parts
    if "projects" in parts:
        try:
            encoded = parts[parts.index("projects") + 1]
        except (ValueError, IndexError):
            return None
        encoded = encoded.lstrip("-")
        if "-git-" in encoded:
            return encoded.split("-git-", 1)[1] or None
        if encoded:
            return encoded.rsplit("-", 1)[-1] or None
    return None


def _recent_touch_entry(source_path: str, provider: str, last_updated: str) -> dict[str, Any]:
    path = Path(source_path)
    cwd, branch = _read_session_context(path)
    workspace_label = _derive_workspace_label(path, cwd=cwd)
    return {
        "provider": provider,
        "last_updated": last_updated,
        "workspace_label": workspace_label,
        "branch": branch,
        "is_subagent": "subagents" in path.parts,
    }


def _collect_local_config(claude_dir: Path) -> dict[str, Any]:
    url_path = claude_dir / "longhouse-url"
    machine_name_path = claude_dir / "longhouse-machine-name"
    return {
        "url_path": str(url_path),
        "machine_name_path": str(machine_name_path),
        "stored_url": _read_trimmed_file(url_path),
        "machine_name": _read_trimmed_file(machine_name_path),
    }


def _candidate_runner_env_paths() -> list[Path]:
    paths = [Path.home() / ".config" / "longhouse" / "runner.env"]
    if os.name != "nt":
        paths.append(Path("/etc/longhouse/runner.env"))
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip("\"'")
        if normalized_key:
            payload[normalized_key] = normalized_value
    return payload


def _collect_runner_config() -> dict[str, Any]:
    for path in _candidate_runner_env_paths():
        if not path.exists():
            continue
        try:
            env = _parse_env_file(path)
        except OSError as exc:
            return {
                "path": str(path),
                "exists": True,
                "error": str(exc),
                "runner_name": None,
                "runner_id": None,
                "runner_urls": [],
                "install_mode": None,
            }

        urls: list[str] = []
        raw_urls = str(env.get("LONGHOUSE_URLS") or "").strip()
        if raw_urls:
            urls = [item.strip() for item in raw_urls.split(",") if item.strip()]
        else:
            raw_url = str(env.get("LONGHOUSE_URL") or "").strip()
            if raw_url:
                urls = [raw_url]

        return {
            "path": str(path),
            "exists": True,
            "error": None,
            "runner_name": str(env.get("RUNNER_NAME") or "").strip() or None,
            "runner_id": str(env.get("RUNNER_ID") or "").strip() or None,
            "runner_urls": urls,
            "install_mode": str(env.get("RUNNER_INSTALL_MODE") or "").strip() or None,
        }

    return {
        "path": str(_candidate_runner_env_paths()[0]),
        "exists": False,
        "error": None,
        "runner_name": None,
        "runner_id": None,
        "runner_urls": [],
        "install_mode": None,
    }


def _extract_machine_name_from_args(arguments: list[str]) -> str | None:
    for index, arg in enumerate(arguments[:-1]):
        if arg == "--machine-name":
            candidate = str(arguments[index + 1] or "").strip()
            return candidate or None
    return None


def _extract_service_machine_name(service_file: str | None) -> str | None:
    raw = str(service_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.exists():
        return None

    try:
        if path.suffix == ".plist":
            payload = plistlib.loads(path.read_bytes())
            arguments = [str(item) for item in payload.get("ProgramArguments") or []]
            return _extract_machine_name_from_args(arguments)

        if path.suffix == ".service":
            for raw_line in path.read_text().splitlines():
                line = raw_line.strip()
                if not line.startswith("ExecStart="):
                    continue
                arguments = shlex.split(line.split("=", 1)[1].strip())
                return _extract_machine_name_from_args(arguments)
    except Exception:
        return None

    return None


def _collect_launch_readiness(claude_dir: Path, *, service: dict[str, Any]) -> dict[str, Any]:
    config = _collect_local_config(claude_dir)
    runner = _collect_runner_config()
    service_machine_name = _extract_service_machine_name(service.get("service_file"))
    reasons: list[str] = []
    actions: list[str] = []

    stored_url = str(config.get("stored_url") or "").strip() or None
    machine_name = str(config.get("machine_name") or "").strip() or None
    runner_name = str(runner.get("runner_name") or "").strip() or None
    runner_urls = [str(item).strip() for item in list(runner.get("runner_urls") or []) if str(item).strip()]

    if stored_url and runner_urls and stored_url not in runner_urls:
        reasons.append("config_url_runner_url_mismatch")
        _with_action(
            actions,
            f"Run: longhouse connect --install --url {runner_urls[0]} --machine-name {runner_name or machine_name or 'this-machine'}",
        )

    if machine_name and runner_name and machine_name != runner_name:
        reasons.append("machine_name_runner_name_mismatch")
        _with_action(
            actions,
            f"Run: longhouse connect --install --url {stored_url or (runner_urls[0] if runner_urls else 'https://<your-longhouse>')} --machine-name {runner_name}",
        )

    if machine_name and service_machine_name and machine_name != service_machine_name:
        reasons.append("service_machine_name_mismatch")
        _with_action(actions, "Run: longhouse connect --install")

    if runner_name and service_machine_name and runner_name != service_machine_name:
        reasons.append("service_runner_name_mismatch")
        _with_action(actions, "Run: longhouse connect --install")

    configured = bool(stored_url or machine_name or service_machine_name or runner.get("exists"))

    if reasons:
        state = "broken"
        headline = "Managed launch config is inconsistent"
    elif configured:
        state = "ready"
        headline = "Managed launch configuration looks coherent"
    else:
        state = "unconfigured"
        headline = "Managed launch has not been configured on this machine"

    return {
        "state": state,
        "headline": headline,
        "reasons": reasons,
        "suggested_actions": actions,
        "stored_url": stored_url,
        "machine_name": machine_name,
        "service_machine_name": service_machine_name,
        "runner": runner,
    }


def _collect_engine_status(claude_dir: Path, *, now: datetime) -> dict[str, Any]:
    status_path = claude_dir / "engine-status.json"
    if not status_path.exists():
        return {
            "path": str(status_path),
            "exists": False,
            "fresh": False,
            "age_seconds": None,
            "payload": None,
            "error": None,
        }

    try:
        age_seconds = int(max(0.0, now.timestamp() - status_path.stat().st_mtime))
    except OSError as exc:
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": None,
            "payload": None,
            "error": str(exc),
        }

    try:
        payload = json.loads(status_path.read_text())
    except Exception as exc:
        return {
            "path": str(status_path),
            "exists": True,
            "fresh": False,
            "age_seconds": age_seconds,
            "payload": None,
            "error": str(exc),
        }

    return {
        "path": str(status_path),
        "exists": True,
        "fresh": age_seconds <= ENGINE_FRESH_SECONDS,
        "age_seconds": age_seconds,
        "payload": payload,
        "error": None,
    }


def _collect_outbox(claude_dir: Path, *, now: datetime) -> dict[str, Any]:
    outbox_dir = claude_dir / "outbox"
    if not outbox_dir.exists():
        return {
            "path": str(outbox_dir),
            "file_count": 0,
            "oldest_age_seconds": None,
        }

    files = [path for path in outbox_dir.iterdir() if path.is_file() and path.name.endswith(".json") and not path.name.startswith(".")]
    if not files:
        return {
            "path": str(outbox_dir),
            "file_count": 0,
            "oldest_age_seconds": None,
        }

    oldest_age_seconds: int | None = None
    for path in files:
        try:
            age_seconds = int(max(0.0, now.timestamp() - path.stat().st_mtime))
        except OSError:
            continue
        oldest_age_seconds = age_seconds if oldest_age_seconds is None else max(oldest_age_seconds, age_seconds)

    return {
        "path": str(outbox_dir),
        "file_count": len(files),
        "oldest_age_seconds": oldest_age_seconds,
    }


def _collect_service() -> dict[str, Any]:
    return get_service_info()


def _collect_version_info() -> dict[str, Any] | None:
    """Read cached PyPI update check — no network call, cache only."""
    try:
        # Import lazily so the service layer does not take a hard module-import
        # dependency on CLI startup order.
        from zerg.cli.update_manager import load_update_cache

        cached = load_update_cache()
    except Exception:
        return None
    if cached is None:
        return None
    return {
        "installed_version": cached.installed_version,
        "latest_version": cached.latest_version,
        "update_available": cached.update_available,
        "upgrade_command": cached.upgrade_command,
        "checked_at": cached.checked_at,
    }


def _collect_activity_summary(claude_dir: Path, *, now: datetime) -> dict[str, Any]:
    db_path = claude_dir / "longhouse-shipper.db"
    summary = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "error": None,
        "sessions_today": 0,
        "sessions_recent": 0,
        "provider_counts_today": {},
        "provider_counts_recent": {},
        "session_recency_bands": [],
        "recent_touches": [],
        "latest_activity_at": None,
        "recent_window_minutes": ACTIVITY_RECENT_MINUTES,
    }
    if not db_path.exists():
        return summary

    local_now = now.astimezone()
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    recent_cutoff_utc = now - timedelta(minutes=ACTIVITY_RECENT_MINUTES)
    today_cutoff = _to_rfc3339(start_of_day_utc)
    recent_cutoff = _to_rfc3339(recent_cutoff_utc)
    band_edges = [_to_rfc3339(now - delta) for _, delta in ACTIVITY_RECENCY_BANDS]
    session_expr = "provider || ':' || COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    provider_session_expr = "COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    session_files_predicate = "path LIKE '%.jsonl'"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary

    try:
        aggregate_row = conn.execute(
            f"""
            SELECT
                MAX(last_updated),
                COUNT(DISTINCT CASE
                    WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
                END),
                COUNT(DISTINCT CASE
                    WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
                END)
            FROM file_state
            WHERE {session_files_predicate}
            """,
            (today_cutoff, recent_cutoff),
        ).fetchone()
        if aggregate_row is not None:
            summary["latest_activity_at"] = aggregate_row[0]
            summary["sessions_today"] = int(aggregate_row[1] or 0)
            summary["sessions_recent"] = int(aggregate_row[2] or 0)

        provider_counts_today: dict[str, int] = {}
        for provider, count in conn.execute(
            f"""
            SELECT provider, COUNT(DISTINCT {provider_session_expr})
            FROM file_state
            WHERE {session_files_predicate}
              AND julianday(last_updated) >= julianday(?)
            GROUP BY provider
            """,
            (today_cutoff,),
        ):
            provider_name = str(provider or "").strip()
            if not provider_name:
                continue
            provider_counts_today[provider_name] = int(count or 0)
        summary["provider_counts_today"] = provider_counts_today

        provider_counts_recent: dict[str, int] = {}
        for provider, count in conn.execute(
            f"""
            SELECT provider, COUNT(DISTINCT {provider_session_expr})
            FROM file_state
            WHERE {session_files_predicate}
              AND julianday(last_updated) >= julianday(?)
            GROUP BY provider
            """,
            (recent_cutoff,),
        ):
            provider_name = str(provider or "").strip()
            if not provider_name:
                continue
            provider_counts_recent[provider_name] = int(count or 0)
        summary["provider_counts_recent"] = provider_counts_recent

        band_specs = [
            {"label": "0-1m", "newer_than": band_edges[0], "older_than": None},
            {"label": "1-5m", "newer_than": band_edges[1], "older_than": band_edges[0]},
            {"label": "5-15m", "newer_than": band_edges[2], "older_than": band_edges[1]},
            {"label": "15-60m", "newer_than": band_edges[3], "older_than": band_edges[2]},
            {"label": "1-6h", "newer_than": band_edges[4], "older_than": band_edges[3]},
            {"label": "6h+", "newer_than": today_cutoff, "older_than": band_edges[4]},
        ]
        band_clauses: list[str] = []
        band_params: list[str] = []
        for spec in band_specs:
            clause = "COUNT(DISTINCT CASE WHEN julianday(last_updated) >= julianday(?)"
            band_params.append(spec["newer_than"])
            older_than = spec["older_than"]
            if older_than is not None:
                clause += " AND julianday(last_updated) < julianday(?)"
                band_params.append(older_than)
            clause += f" THEN {session_expr} END)"
            band_clauses.append(clause)

        band_row = conn.execute(
            f"""
            SELECT {", ".join(band_clauses)}
            FROM file_state
            WHERE {session_files_predicate}
            """,
            tuple(band_params),
        ).fetchone()
        if band_row is not None:
            summary["session_recency_bands"] = [
                {
                    "label": spec["label"],
                    "session_count": int(band_row[index] or 0),
                }
                for index, spec in enumerate(band_specs)
            ]

        recent_touches: list[dict[str, Any]] = []
        for provider, last_updated, path in conn.execute(
            f"""
            SELECT provider, last_updated, path
            FROM (
                SELECT
                    provider,
                    {provider_session_expr} AS session_key,
                    path,
                    MAX(last_updated) AS last_updated
                FROM file_state
                WHERE {session_files_predicate}
                GROUP BY provider, session_key
            )
            ORDER BY julianday(last_updated) DESC
            LIMIT ?
            """,
            (RECENT_TOUCH_LIMIT,),
        ):
            provider_name = str(provider or "").strip()
            if not provider_name or not last_updated:
                continue
            recent_touches.append(_recent_touch_entry(str(path), provider_name, str(last_updated)))
        summary["recent_touches"] = recent_touches
        return summary
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        conn.close()


def _with_action(actions: list[str], text: str) -> None:
    if text not in actions:
        actions.append(text)


def _classify_health(
    *,
    service: dict[str, Any],
    engine_status: dict[str, Any],
    outbox: dict[str, Any],
    launch_readiness: dict[str, Any],
) -> tuple[str, str, str, list[str], list[str]]:
    reasons: list[str] = []
    actions: list[str] = []

    service_status = str(service.get("status") or "not-installed")
    payload = engine_status.get("payload") or {}
    engine_exists = bool(engine_status.get("exists"))
    engine_error = engine_status.get("error")
    engine_age = engine_status.get("age_seconds")
    spool_pending = int(payload.get("spool_pending_count") or 0)
    spool_dead = int(payload.get("spool_dead_count") or 0)
    ship_failures = int(payload.get("consecutive_ship_failures") or 0)
    parse_errors = int(payload.get("parse_error_count_1h") or 0)
    is_offline = bool(payload.get("is_offline") or False)
    disk_free_bytes = payload.get("disk_free_bytes")
    outbox_count = int(outbox.get("file_count") or 0)
    outbox_oldest = outbox.get("oldest_age_seconds")
    launch_state = str(launch_readiness.get("state") or "unconfigured")
    launch_reasons = [str(item) for item in list(launch_readiness.get("reasons") or [])]
    launch_actions = [str(item) for item in list(launch_readiness.get("suggested_actions") or [])]

    reasons.extend(launch_reasons)
    for action in launch_actions:
        _with_action(actions, action)

    if service_status == "not-installed":
        reasons.append("service_not_installed")
        _with_action(actions, "Run: longhouse connect --install")
    elif service_status == "stopped":
        reasons.append("service_stopped")
        _with_action(actions, "Run: longhouse connect --install")

    if engine_error:
        reasons.append("engine_status_unreadable")
        _with_action(actions, "Inspect: ~/.claude/engine-status.json")
    elif not engine_exists:
        reasons.append("engine_status_missing")
        if service_status == "running":
            _with_action(actions, "Wait for the first local status update or inspect engine logs")
        else:
            _with_action(actions, "Run: longhouse connect --install")
    elif engine_age is not None and engine_age > ENGINE_STALE_SECONDS:
        reasons.append("engine_status_stale")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")
    elif engine_age is not None and engine_age > ENGINE_FRESH_SECONDS:
        reasons.append("engine_status_aging")

    if is_offline:
        reasons.append("engine_offline")
        _with_action(actions, "Verify network reachability to your Longhouse URL")

    if ship_failures > 0:
        reasons.append("ship_failures")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")

    if parse_errors > 0:
        reasons.append("parse_errors")
        _with_action(actions, "Inspect recent dead letters and parser errors")

    if spool_pending >= DEGRADED_BACKLOG_COUNT:
        reasons.append("spool_pending")

    if spool_dead > 0:
        reasons.append("spool_dead")
        _with_action(actions, "Repair dead letters before trusting continuity")

    if outbox_count >= DEGRADED_BACKLOG_COUNT:
        reasons.append("outbox_backlog")
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
        reasons.append("outbox_stuck")
        _with_action(actions, "Inspect logs: ~/.claude/logs/engine.log.*")

    if isinstance(disk_free_bytes, int):
        if disk_free_bytes < DISK_BROKEN_BYTES:
            reasons.append("disk_critically_low")
            _with_action(actions, "Free local disk space before continuing to rely on shipping")
        elif disk_free_bytes < DISK_DEGRADED_BYTES:
            reasons.append("disk_low")
            _with_action(actions, "Consider freeing disk space soon")

    if service_status == "not-installed" and not engine_exists and outbox_count == 0 and spool_pending == 0 and launch_state != "broken":
        return (
            "uninstalled",
            "gray",
            "Longhouse local shipping is not installed",
            reasons,
            actions,
        )

    broken = False
    degraded = False

    if launch_state == "broken":
        broken = True
    elif launch_state == "degraded":
        degraded = True

    if service_status == "stopped":
        broken = True
    if engine_error:
        broken = True
    if spool_dead > 0:
        broken = True
    if isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_BROKEN_BYTES:
        broken = True
    if outbox_count >= BROKEN_BACKLOG_COUNT:
        broken = True
    if outbox_count > 0 and outbox_oldest is not None and outbox_oldest > OUTBOX_BROKEN_AGE_SECONDS:
        broken = True
    if spool_pending >= BROKEN_BACKLOG_COUNT:
        broken = True
    if service_status != "running" and (outbox_count > 0 or spool_pending > 0):
        broken = True
    if engine_exists and engine_age is not None and engine_age > ENGINE_STALE_SECONDS and (outbox_count > 0 or spool_pending > 0):
        broken = True

    if not broken:
        if service_status != "running":
            degraded = True
        if not engine_exists:
            degraded = True
        if engine_age is not None and engine_age > ENGINE_FRESH_SECONDS:
            degraded = True
        if is_offline or ship_failures > 0 or parse_errors > 0:
            degraded = True
        if spool_pending >= DEGRADED_BACKLOG_COUNT:
            degraded = True
        if outbox_count >= DEGRADED_BACKLOG_COUNT and outbox_oldest is not None and outbox_oldest > OUTBOX_DEGRADED_AGE_SECONDS:
            degraded = True
        if isinstance(disk_free_bytes, int) and disk_free_bytes < DISK_DEGRADED_BYTES:
            degraded = True

    if broken:
        headline = "Longhouse shipping needs repair"
        if any(
            reason in reasons
            for reason in (
                "config_url_runner_url_mismatch",
                "machine_name_runner_name_mismatch",
                "service_machine_name_mismatch",
                "service_runner_name_mismatch",
            )
        ):
            headline = "Longhouse launch config is inconsistent"
        elif "service_stopped" in reasons:
            headline = "Longhouse engine service is stopped"
        elif "spool_dead" in reasons:
            headline = "Longhouse has dead-lettered data to repair"
        elif "engine_status_stale" in reasons:
            headline = "Longhouse local status is stale while work is pending"
        return ("broken", "red", headline, reasons, actions)

    if degraded:
        headline = "Longhouse shipping is degraded"
        if "engine_offline" in reasons:
            headline = "Longhouse is retrying while offline"
        elif "engine_status_missing" in reasons and service_status == "running":
            headline = "Longhouse is waiting for its first local status update"
        elif "engine_status_stale" in reasons:
            headline = "Longhouse local status is aging"
        elif "engine_status_aging" in reasons:
            headline = "Longhouse local status is aging"
        return ("degraded", "yellow", headline, reasons, actions)

    return ("healthy", "green", "Longhouse shipping healthy", reasons, actions)


def collect_local_health(claude_dir: str | Path | None = None) -> dict[str, Any]:
    now = _utc_now()
    resolved_claude_dir = _coerce_path(claude_dir)
    service = _collect_service()
    engine_status = _collect_engine_status(resolved_claude_dir, now=now)
    outbox = _collect_outbox(resolved_claude_dir, now=now)
    activity_summary = _collect_activity_summary(resolved_claude_dir, now=now)
    launch_readiness = _collect_launch_readiness(resolved_claude_dir, service=service)
    health_state, severity, headline, reasons, suggested_actions = _classify_health(
        service=service,
        engine_status=engine_status,
        outbox=outbox,
        launch_readiness=launch_readiness,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": _to_rfc3339(now),
        "health_state": health_state,
        "severity": severity,
        "headline": headline,
        "reasons": reasons,
        "suggested_actions": suggested_actions,
        "service": service,
        "engine_status": engine_status,
        "outbox": outbox,
        "activity_summary": activity_summary,
        "launch_readiness": launch_readiness,
        "update_info": _collect_version_info(),
        "thresholds": {
            "engine_fresh_seconds": ENGINE_FRESH_SECONDS,
            "engine_stale_seconds": ENGINE_STALE_SECONDS,
            "outbox_degraded_age_seconds": OUTBOX_DEGRADED_AGE_SECONDS,
            "outbox_broken_age_seconds": OUTBOX_BROKEN_AGE_SECONDS,
            "degraded_backlog_count": DEGRADED_BACKLOG_COUNT,
            "broken_backlog_count": BROKEN_BACKLOG_COUNT,
            "disk_degraded_bytes": DISK_DEGRADED_BYTES,
            "disk_broken_bytes": DISK_BROKEN_BYTES,
        },
    }
