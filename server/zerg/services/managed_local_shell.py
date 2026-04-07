"""Shared shell bootstrap for managed-local commands."""

from __future__ import annotations

import shlex

MANAGED_LOCAL_STANDARD_PATH_PREFIXES = (
    "$HOME/.local/bin",
    "$HOME/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/sbin",
)


def _quote(value: str) -> str:
    return shlex.quote(value)


def build_managed_local_path_export() -> str:
    """Prepend common user-local install locations without loading interactive shell state."""
    joined = ":".join(MANAGED_LOCAL_STANDARD_PATH_PREFIXES)
    return f'export PATH="{joined}:$PATH"'


def build_managed_local_conditional_zshrc_source(*, required_commands: tuple[str, ...] = ()) -> str | None:
    """Source ~/.zshrc only when fast-path PATH resolution still misses a required binary."""
    cleaned_commands = (str(command or "").strip() for command in required_commands)
    normalized = tuple(dict.fromkeys(command for command in cleaned_commands if command))
    if not normalized:
        return None

    missing_checks = " || ".join(f"! command -v {_quote(command)} >/dev/null 2>&1" for command in normalized)
    return f"if {missing_checks}; then source ~/.zshrc >/dev/null 2>&1 || true; fi"


def build_managed_local_shell_prelude(*, required_commands: tuple[str, ...] = ()) -> str:
    """Shell bootstrap shared by native managed-local commands."""
    commands = [build_managed_local_path_export()]
    zshrc_fallback = build_managed_local_conditional_zshrc_source(required_commands=required_commands)
    if zshrc_fallback:
        commands.append(zshrc_fallback)
    return "; ".join(commands)


__all__ = [
    "build_managed_local_conditional_zshrc_source",
    "build_managed_local_path_export",
    "build_managed_local_shell_prelude",
]
