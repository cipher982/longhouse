"""Privacy-preserving acquisition telemetry for install and first-run signals."""

from __future__ import annotations

import json
import os
import platform
import threading
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from zerg.services.longhouse_paths import resolve_longhouse_home

DEFAULT_TELEMETRY_ENDPOINT = "https://control.longhouse.ai/api/acquisition/events"


def telemetry_enabled() -> bool:
    raw = os.getenv("LONGHOUSE_TELEMETRY", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if os.getenv("DO_NOT_TRACK", "").strip() == "1":
        return False
    if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return True


def _install_id_path() -> Path:
    return resolve_longhouse_home() / "install-id"


def _once_marker_path() -> Path:
    return resolve_longhouse_home() / "acquisition-events.json"


def load_or_create_install_id() -> str:
    path = _install_id_path()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing[:128]
        path.parent.mkdir(parents=True, exist_ok=True)
        install_id = str(uuid.uuid4())
        path.write_text(install_id + "\n", encoding="utf-8")
        return install_id
    except OSError:
        return str(uuid.uuid4())


def _installed_version() -> str | None:
    try:
        return metadata.version("longhouse")
    except metadata.PackageNotFoundError:
        return None


def _post(payload: dict[str, Any]) -> bool:
    endpoint = os.getenv("LONGHOUSE_TELEMETRY_ENDPOINT", DEFAULT_TELEMETRY_ENDPOINT).strip()
    if not endpoint:
        return False
    try:
        response = httpx.post(
            endpoint,
            json=payload,
            timeout=1.5,
            headers={"User-Agent": f"longhouse-cli/{payload.get('version') or 'unknown'}"},
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def emit_acquisition_event(
    event_name: str,
    *,
    command: str | None = None,
    source: str | None = "cli",
    topology: str | None = None,
    install_method: str | None = None,
    install_source: str | None = None,
    channel: str | None = None,
    props: dict[str, Any] | None = None,
    background: bool = True,
) -> bool:
    if not telemetry_enabled():
        return False

    payload = {
        "event_name": event_name,
        "install_id": load_or_create_install_id(),
        "source": source,
        "version": _installed_version(),
        "os_name": platform.system().lower() or None,
        "arch": platform.machine().lower() or None,
        "command": command,
        "install_method": install_method,
        "install_source": install_source,
        "channel": channel,
        "topology": topology,
        "ci": False,
        "props": props or {},
    }

    if background:
        threading.Thread(target=_post, args=(payload,), daemon=True).start()
        return True
    else:
        return _post(payload)


def emit_acquisition_event_once(event_key: str, event_name: str, **kwargs: Any) -> None:
    if not telemetry_enabled():
        return

    path = _once_marker_path()
    try:
        if path.exists():
            seen = set(json.loads(path.read_text(encoding="utf-8")))
        else:
            seen = set()
        if event_key in seen:
            return
        if not emit_acquisition_event(event_name, **{**kwargs, "background": False}):
            return
        seen.add(event_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(seen), indent=2) + "\n", encoding="utf-8")
    except Exception:
        emit_acquisition_event(event_name, **kwargs)


def emit_install_metadata_event(metadata_payload: Any) -> None:
    props: dict[str, Any] = {}
    package_ref = getattr(metadata_payload, "package_ref", None)
    if package_ref:
        package_ref_text = str(package_ref)
        if package_ref_text.startswith("longhouse=="):
            props["package_ref_kind"] = "pypi_version"
        elif package_ref_text.startswith(("http://", "https://", "git+")):
            props["package_ref_kind"] = "url"
        elif package_ref_text.startswith(("/", "./", "../")):
            props["package_ref_kind"] = "local_path"
        else:
            props["package_ref_kind"] = "custom"

    emit_acquisition_event(
        "install_success",
        command="record_install",
        source="installer",
        install_method=getattr(metadata_payload, "install_method", None),
        install_source=getattr(metadata_payload, "install_source", None),
        channel=getattr(metadata_payload, "channel", None),
        props=props,
        background=False,
    )


def telemetry_notice() -> str:
    return (
        "Anonymous install telemetry is enabled. "
        "Set LONGHOUSE_TELEMETRY=0 or DO_NOT_TRACK=1 to disable. "
        "No prompts, paths, secrets, or session contents are sent."
    )
