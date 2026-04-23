"""Canonical non-secret machine install state for Longhouse local runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from zerg.services.longhouse_paths import get_machine_state_journal_path
from zerg.services.longhouse_paths import get_machine_state_path

SCHEMA_VERSION = 1
_MISSING = object()


@dataclass(frozen=True)
class MachineState:
    schema_version: int = SCHEMA_VERSION
    config_generation: str | None = None
    runtime_url: str | None = None
    machine_name: str | None = None
    topology_intent: str | None = None
    desktop_app_enabled: bool | None = None
    runner_enabled: bool | None = None
    desired_bundle_version: str | None = None
    written_by: str | None = None
    written_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_generation": self.config_generation,
            "runtime_url": self.runtime_url,
            "machine_name": self.machine_name,
            "topology_intent": self.topology_intent,
            "desktop_app_enabled": self.desktop_app_enabled,
            "runner_enabled": self.runner_enabled,
            "desired_bundle_version": self.desired_bundle_version,
            "written_by": self.written_by,
            "written_at": self.written_at,
        }


def normalize_runtime_url(url: object | None) -> str | None:
    """Return a valid Longhouse runtime URL or None."""
    if not isinstance(url, str):
        return None

    normalized = url.strip()
    if not normalized:
        return None
    if "typer.models.OptionInfo" in normalized or "<" in normalized or ">" in normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        return None

    return normalized


def sanitize_machine_name(name: object | None) -> str | None:
    """Sanitize a machine name for service args and user-facing labels."""
    if not isinstance(name, str):
        return None

    normalized = name.strip()
    if not normalized:
        return None

    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[&<>\"']", "", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized.strip("-")
    return normalized[:64] or None


def read_machine_state(base_dir: Path | None = None) -> tuple[Path, MachineState | None, str | None]:
    """Read the canonical machine state file.

    Returns ``(path, state, error)``. Missing files return ``(path, None, None)``.
    """
    path = get_machine_state_path(base_dir)
    if not path.exists():
        return path, None, None

    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        return path, None, str(exc)

    if not isinstance(payload, dict):
        return path, None, "machine state must be a JSON object"

    return path, _machine_state_from_payload(payload), None


def load_machine_state(base_dir: Path | None = None) -> MachineState | None:
    """Return the canonical machine state when present and parseable."""
    _path, state, _error = read_machine_state(base_dir)
    return state


def write_machine_state(
    *,
    base_dir: Path | None = None,
    written_by: str,
    runtime_url: object = _MISSING,
    machine_name: object = _MISSING,
    topology_intent: object = _MISSING,
    desktop_app_enabled: object = _MISSING,
    runner_enabled: object = _MISSING,
    desired_bundle_version: object = _MISSING,
) -> MachineState:
    """Persist canonical machine state and append a provenance journal entry."""
    if not str(written_by or "").strip():
        raise ValueError("written_by is required")

    state_path, current_state, error = read_machine_state(base_dir)
    if error:
        raise RuntimeError(f"Failed to read existing machine state at {state_path}: {error}")

    written_at = _to_rfc3339(datetime.now(timezone.utc))
    draft_state = MachineState(
        schema_version=SCHEMA_VERSION,
        config_generation=current_state.config_generation if current_state else None,
        runtime_url=_resolve_runtime_url(runtime_url, current_state),
        machine_name=_resolve_machine_name(machine_name, current_state),
        topology_intent=_resolve_text(topology_intent, current_state.topology_intent if current_state else None),
        desktop_app_enabled=_resolve_bool(desktop_app_enabled, current_state.desktop_app_enabled if current_state else None),
        runner_enabled=_resolve_bool(runner_enabled, current_state.runner_enabled if current_state else None),
        desired_bundle_version=_resolve_text(
            desired_bundle_version,
            current_state.desired_bundle_version if current_state else None,
        ),
        written_by=str(written_by).strip(),
        written_at=written_at,
    )
    next_generation = _new_generation(written_at)
    if (
        current_state
        and current_state.config_generation
        and machine_state_source_hash(current_state) == machine_state_source_hash(draft_state)
    ):
        next_generation = current_state.config_generation

    next_state = MachineState(
        schema_version=draft_state.schema_version,
        config_generation=next_generation,
        runtime_url=draft_state.runtime_url,
        machine_name=draft_state.machine_name,
        topology_intent=draft_state.topology_intent,
        desktop_app_enabled=draft_state.desktop_app_enabled,
        runner_enabled=draft_state.runner_enabled,
        desired_bundle_version=draft_state.desired_bundle_version,
        written_by=draft_state.written_by,
        written_at=draft_state.written_at,
    )

    serialized = json.dumps(next_state.to_dict(), indent=2, sort_keys=True) + "\n"
    _write_text_atomic(state_path, serialized)
    _append_state_journal(
        journal_path=get_machine_state_journal_path(base_dir),
        old_state=current_state,
        new_state=next_state,
    )
    return next_state


def clear_machine_runtime_url(base_dir: Path | None = None, *, written_by: str) -> bool:
    """Clear only the canonical runtime URL field."""
    _path, current_state, error = read_machine_state(base_dir)
    if error:
        raise RuntimeError(error)
    if current_state is None or current_state.runtime_url is None:
        return False

    write_machine_state(
        base_dir=base_dir,
        written_by=written_by,
        runtime_url=None,
    )
    return True


def machine_state_source_hash(state: MachineState | None) -> str | None:
    """Return a stable hash of durable machine-state facts."""
    if state is None:
        return None

    payload = {
        "schema_version": state.schema_version,
        "runtime_url": state.runtime_url,
        "machine_name": state.machine_name,
        "topology_intent": state.topology_intent,
        "desktop_app_enabled": state.desktop_app_enabled,
        "runner_enabled": state.runner_enabled,
        "desired_bundle_version": state.desired_bundle_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _machine_state_from_payload(payload: dict[str, object]) -> MachineState:
    schema_version = payload.get("schema_version")
    return MachineState(
        schema_version=schema_version if isinstance(schema_version, int) else SCHEMA_VERSION,
        config_generation=_normalize_text(payload.get("config_generation")),
        runtime_url=normalize_runtime_url(payload.get("runtime_url")),
        machine_name=sanitize_machine_name(payload.get("machine_name")),
        topology_intent=_normalize_text(payload.get("topology_intent")),
        desktop_app_enabled=payload.get("desktop_app_enabled") if isinstance(payload.get("desktop_app_enabled"), bool) else None,
        runner_enabled=payload.get("runner_enabled") if isinstance(payload.get("runner_enabled"), bool) else None,
        desired_bundle_version=_normalize_text(payload.get("desired_bundle_version")),
        written_by=_normalize_text(payload.get("written_by")),
        written_at=_normalize_text(payload.get("written_at")),
    )


def _resolve_runtime_url(value: object, current_state: MachineState | None) -> str | None:
    if value is _MISSING:
        return current_state.runtime_url if current_state else None
    if value is None:
        return None
    normalized = normalize_runtime_url(value)
    if normalized is None:
        raise ValueError(f"Invalid Longhouse URL: {value!r}")
    return normalized


def _resolve_machine_name(value: object, current_state: MachineState | None) -> str | None:
    if value is _MISSING:
        return current_state.machine_name if current_state else None
    if value is None:
        return None
    normalized = sanitize_machine_name(value)
    if normalized is None:
        raise ValueError(f"Invalid machine name: {value!r}")
    return normalized


def _resolve_text(value: object, current: str | None) -> str | None:
    if value is _MISSING:
        return current
    if value is None:
        return None
    return _normalize_text(value)


def _resolve_bool(value: object, current: bool | None) -> bool | None:
    if value is _MISSING:
        return current
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Expected bool, got {type(value).__name__}")
    return value


def _normalize_text(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _new_generation(written_at: str) -> str:
    prefix = written_at.replace(":", "").replace("-", "")
    return f"{prefix}-{uuid4().hex[:8]}"


def _append_state_journal(*, journal_path: Path, old_state: MachineState | None, new_state: MachineState) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "written_at": new_state.written_at,
        "written_by": new_state.written_by,
        "config_generation": new_state.config_generation,
        "pid": os.getpid(),
        "cwd": _safe_getcwd(),
        "argv": list(sys.argv),
        "old": old_state.to_dict() if old_state else None,
        "new": new_state.to_dict(),
    }
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _safe_getcwd() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


def _to_rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        path.write_text(content)
        return

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    closed = False
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.rename(tmp_path, path)
    except Exception:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
