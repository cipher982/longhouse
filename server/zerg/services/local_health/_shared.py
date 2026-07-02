from __future__ import annotations

import os
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_MISSING
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.longhouse_paths import resolve_longhouse_home


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_rfc3339(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _max_rfc3339(*values: str | None) -> str | None:
    candidates = [_parse_rfc3339(value) for value in values]
    present = [value for value in candidates if value is not None]
    if not present:
        return None
    return _to_rfc3339(max(present))


def _coerce_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return resolve_longhouse_home()


def _canonical_stable_home() -> Path:
    return (Path.home() / ".longhouse").expanduser().resolve(strict=False)


def _read_trimmed_file(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def _normalize_optional_string(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _normalize_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_provider_cli_candidate(candidate: str | None) -> str | None:
    normalized = _normalize_optional_string(candidate)
    if normalized is None:
        return None
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(normalized).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(normalized)


def _provider_cli_reference(path: str | None, *, source: str) -> dict[str, str | None]:
    return {"path": _normalize_optional_string(path), "source": source}


def _collect_provider_cli(*, binary: str, env_var: str | None) -> dict[str, Any]:
    env_candidate = _normalize_optional_string(os.environ.get(env_var)) if env_var else None
    if env_candidate:
        path = _resolve_provider_cli_candidate(env_candidate)
        source = env_var
        resolution_error = None if path else f"{env_var} did not resolve to an executable"
    else:
        path = shutil.which(binary)
        source = PROVIDER_CLI_SOURCE_PATH if path else PROVIDER_CLI_SOURCE_MISSING
        resolution_error = None if path else f"`{binary}` not found on PATH"
    return {
        "path": path,
        "source": source,
        "resolution_error": resolution_error,
        "env_override": env_candidate,
    }


def _collect_provider_clis() -> dict[str, Any]:
    return {
        provider: _collect_provider_cli(
            binary=binary,
            env_var=PROVIDER_CLI_ENV_BY_PROVIDER.get(provider),
        )
        for provider, binary in PROVIDER_CLI_BINARY_BY_PROVIDER.items()
    }


def _with_action(actions: list[str], text: str) -> None:
    if text not in actions:
        actions.append(text)


__all__ = [
    "_utc_now",
    "_to_rfc3339",
    "_parse_rfc3339",
    "_max_rfc3339",
    "_coerce_path",
    "_canonical_stable_home",
    "_read_trimmed_file",
    "_normalize_optional_string",
    "_normalize_optional_int",
    "_resolve_provider_cli_candidate",
    "_provider_cli_reference",
    "_collect_provider_cli",
    "_collect_provider_clis",
    "_with_action",
]
