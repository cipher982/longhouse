"""What `longhouse <provider>` commands actually exist.

`config/native_device_entrypoints.json` already governed this for the web
client (`web/src/lib/providers.ts` reads it explicitly, with a comment
explaining why it does not use the capability flags: a provider can support
`launch_local` while its device entrypoint stays `excluded`, and telling a user
to run a command that does not exist is worse than saying nothing).

Nothing on the Python side read it, so `longhouse onboard` suggested
`longhouse agy` — a command that does not exist, for the one provider whose
entrypoint is deliberately excluded — while omitting cursor and opencode
entirely.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]

_STATUS_AVAILABLE = "available"


def _candidates() -> tuple[Path, ...]:
    return (
        REPO_ROOT / "config" / "native_device_entrypoints.json",  # repo / CI checkout
        Path("/config/native_device_entrypoints.json"),  # runtime image
        PACKAGE_ROOT / "_config" / "native_device_entrypoints.json",  # pip wheel
    )


@lru_cache(maxsize=1)
def _load() -> dict:
    for candidate in _candidates():
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {"commands": []}


@lru_cache(maxsize=1)
def available_native_managed_launch_commands() -> tuple[tuple[str, str], ...]:
    """`(provider, command)` for every managed launch entrypoint that ships.

    Excluded entrypoints are omitted: `longhouse antigravity` is declared and
    deliberately not built, so it must never be suggested.
    """

    out: list[tuple[str, str]] = []
    for command in _load().get("commands") or []:
        if not isinstance(command, dict):
            continue
        if str(command.get("status") or "").strip() != _STATUS_AVAILABLE:
            continue
        providers = command.get("providers")
        if not isinstance(providers, list) or len(providers) != 1:
            continue  # `all` / multi-provider commands are not provider launches
        target = str(command.get("native_target_command") or "").strip()
        if not target:
            continue
        out.append((str(providers[0]), target))
    return tuple(sorted(out))


def native_launch_command_for_provider(provider: str) -> str | None:
    for candidate, command in available_native_managed_launch_commands():
        if candidate == provider:
            return command
    return None
