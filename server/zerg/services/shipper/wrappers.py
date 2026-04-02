"""CLI wrapper shims for managed-local sessions.

Installs lightweight shell shims that intercept ``claude`` / ``codex``
invocations and route interactive session launches through
``longhouse claude`` / ``longhouse codex`` while passing everything else
through to the real upstream binary.

Usage:
    from zerg.services.shipper.wrappers import (
        install_wrappers,
        uninstall_wrappers,
        get_wrapper_status,
    )

    install_wrappers()                          # both providers
    install_wrappers(providers=["claude"])       # claude only
    uninstall_wrappers()                        # remove all
    get_wrapper_status()                        # dict per provider
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("claude", "codex")

SHIMS_DIR_NAME = "shims"

# Passthrough subcommands per provider.  Anything matching the first
# positional arg goes straight to the real binary.
_PASSTHROUGH_SUBCOMMANDS: dict[str, set[str]] = {
    "claude": {
        "auth",
        "login",
        "logout",
        "config",
        "mcp",
        "doctor",
        "update",
        "upgrade",
        "help",
        "version",
    },
    "codex": {
        "auth",
        "login",
        "logout",
        "config",
        "completion",
        "update",
        "upgrade",
        "help",
        "version",
    },
}

# Flags that trigger an immediate passthrough regardless of position.
_PASSTHROUGH_FLAGS = {"--help", "-h", "--version", "-v"}

# Flags that indicate non-interactive / pipe mode.
_NONINTERACTIVE_FLAGS = {"-p", "--pipe", "--print", "--print-session-id"}

# Shell profile markers (à la conda / pyenv)
_MARKER_BEGIN = "# >>> longhouse wrapper >>>"
_MARKER_END = "# <<< longhouse wrapper <<<"


def _longhouse_home() -> Path:
    return Path.home() / ".longhouse"


def _shims_dir() -> Path:
    return _longhouse_home() / SHIMS_DIR_NAME


def _get_shell_profile_path() -> Path | None:
    """Primary RC file for the user's login shell."""
    shell_name = os.path.basename(os.environ.get("SHELL", ""))
    home = Path.home()
    if shell_name == "zsh":
        return home / ".zshrc"
    if shell_name == "bash":
        if sys.platform == "darwin":
            return home / ".bash_profile"
        return home / ".bashrc"
    if shell_name == "fish":
        return home / ".config" / "fish" / "config.fish"
    return None


# ------------------------------------------------------------------
# Shim script generation
# ------------------------------------------------------------------


def _build_shim_script(provider: str) -> str:
    """Return a self-contained bash shim for *provider*."""
    passthrough_cmds = "|".join(sorted(_PASSTHROUGH_SUBCOMMANDS.get(provider, set())))
    passthrough_flags = "|".join(sorted(_PASSTHROUGH_FLAGS))
    noninteractive_flags = "|".join(sorted(_NONINTERACTIVE_FLAGS))

    return f"""\
#!/usr/bin/env bash
# Longhouse wrapper shim for {provider}
# Installed by: longhouse wrap --install
# Remove with:  longhouse wrap --uninstall
# Bypass with:  LONGHOUSE_BYPASS=1 {provider} ...
set -euo pipefail

# --- Resolve the real upstream binary (skip this shim) ---
# Canonicalize shims dir to handle symlinks / trailing slashes
_SHIMS_DIR="$(cd "$HOME/.longhouse/shims" 2>/dev/null && pwd -P || echo "$HOME/.longhouse/shims")"
_THIS_SCRIPT="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"
_clean_path() {{
    local IFS=':'
    local _out=""
    for _d in $PATH; do
        # Canonicalize each PATH entry before comparing
        local _canon
        _canon="$(cd "$_d" 2>/dev/null && pwd -P || echo "$_d")"
        [ "$_canon" = "$_SHIMS_DIR" ] && continue
        _out="${{_out:+$_out:}}$_d"
    done
    echo "$_out"
}}
_CLEAN_PATH="$(_clean_path)"
_REAL_BINARY="$(PATH="$_CLEAN_PATH" command -v {provider} 2>/dev/null || true)"

# Guard against self-resolution (symlink, hardlink, etc.)
if [ -n "$_REAL_BINARY" ]; then
    _REAL_CANON="$(cd "$(dirname "$_REAL_BINARY")" 2>/dev/null && pwd -P)/$(basename "$_REAL_BINARY")"
    if [ "$_REAL_CANON" = "$_THIS_SCRIPT" ]; then
        _REAL_BINARY=""
    fi
fi

if [ -z "$_REAL_BINARY" ]; then
    echo "longhouse-wrap: cannot find real '{provider}' binary on PATH" >&2
    exit 127
fi

# --- 1. Bypass ---
if [ "${{LONGHOUSE_BYPASS:-0}}" = "1" ]; then
    exec "$_REAL_BINARY" "$@"
fi

# --- 2. Passthrough known subcommands ---
case "${{1:-}}" in
    {passthrough_cmds})
        exec "$_REAL_BINARY" "$@"
        ;;
    {passthrough_flags})
        exec "$_REAL_BINARY" "$@"
        ;;
esac

# --- 3. Passthrough non-interactive flags ---
for _arg in "$@"; do
    case "$_arg" in
        {noninteractive_flags})
            exec "$_REAL_BINARY" "$@"
            ;;
    esac
done

# --- 4. Passthrough if not a TTY ---
if [ ! -t 0 ] || [ ! -t 1 ]; then
    exec "$_REAL_BINARY" "$@"
fi

# --- 5. Passthrough if any flags present (v1: only bare invocations go managed) ---
if [ $# -gt 0 ]; then
    exec "$_REAL_BINARY" "$@"
fi

# --- 6. Route through Longhouse managed-local ---
exec longhouse {provider}
"""


# ------------------------------------------------------------------
# Shell profile PATH injection
# ------------------------------------------------------------------


def _build_profile_block_posix(shims_dir: Path) -> str:
    # Guard: only prepend if not already present (prevents unbounded PATH growth on re-source)
    return f"{_MARKER_BEGIN}\n" f'case ":$PATH:" in *":{shims_dir}:"*) ;; *) export PATH="{shims_dir}:$PATH" ;; esac\n' f"{_MARKER_END}\n"


def _build_profile_block_fish(shims_dir: Path) -> str:
    # fish_add_path is already idempotent
    return f"{_MARKER_BEGIN}\n" f"fish_add_path --prepend {shims_dir}\n" f"{_MARKER_END}\n"


def _profile_has_block(profile_path: Path) -> bool:
    if not profile_path.exists():
        return False
    text = profile_path.read_text()
    return _MARKER_BEGIN in text and _MARKER_END in text


def _inject_profile_block(profile_path: Path) -> bool:
    """Append the PATH block to the shell profile.  Returns True if modified."""
    if _profile_has_block(profile_path):
        return False

    shell_name = os.path.basename(os.environ.get("SHELL", ""))
    shims_dir = _shims_dir()
    if shell_name == "fish":
        block = _build_profile_block_fish(shims_dir)
    else:
        block = _build_profile_block_posix(shims_dir)

    # Ensure file ends with a newline before our block
    existing = ""
    if profile_path.exists():
        existing = profile_path.read_text()
    separator = "" if existing.endswith("\n") or not existing else "\n"

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("a") as f:
        f.write(f"{separator}\n{block}")
    return True


def _remove_profile_block(profile_path: Path) -> bool:
    """Remove the PATH block from the shell profile.  Returns True if modified."""
    if not profile_path.exists():
        return False
    text = profile_path.read_text()
    if _MARKER_BEGIN not in text:
        return False

    # Remove the marked block (including possible surrounding blank lines)
    pattern = rf"\n?{re.escape(_MARKER_BEGIN)}.*?{re.escape(_MARKER_END)}\n?"
    cleaned = re.sub(pattern, "\n", text, flags=re.DOTALL)
    # Avoid trailing whitespace-only content
    cleaned = cleaned.rstrip() + "\n"
    profile_path.write_text(cleaned)
    return True


# ------------------------------------------------------------------
# Install / uninstall / status
# ------------------------------------------------------------------


def _validate_providers(providers: list[str] | None) -> list[str]:
    if providers is None:
        return list(SUPPORTED_PROVIDERS)
    for p in providers:
        if p not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {p!r} (expected one of {SUPPORTED_PROVIDERS})")
    return providers


def _find_real_binary(provider: str) -> str | None:
    """Find the upstream binary, ignoring our shims dir.

    Does not mutate ``os.environ`` — uses ``shutil.which(path=...)`` instead.
    Canonicalizes the shims dir to handle symlinks / trailing slashes.
    """
    shims_dir = _shims_dir()
    try:
        shims_canon = str(shims_dir.resolve())
    except OSError:
        shims_canon = str(shims_dir)

    original_path = os.environ.get("PATH", "")
    clean_dirs: list[str] = []
    for d in original_path.split(":"):
        try:
            d_canon = str(Path(d).resolve())
        except OSError:
            d_canon = d
        if d_canon == shims_canon:
            continue
        clean_dirs.append(d)
    clean_path = ":".join(clean_dirs)
    return shutil.which(provider, path=clean_path)


def install_wrappers(providers: list[str] | None = None) -> dict[str, str]:
    """Install wrapper shims.

    Returns a dict of ``{provider: status_message}`` for each provider.
    """
    providers = _validate_providers(providers)
    results: dict[str, str] = {}

    shims_dir = _shims_dir()
    shims_dir.mkdir(parents=True, exist_ok=True)

    for provider in providers:
        real_bin = _find_real_binary(provider)
        if not real_bin:
            results[provider] = f"skipped — '{provider}' binary not found on PATH"
            continue

        shim_path = shims_dir / provider
        shim_path.write_text(_build_shim_script(provider))
        shim_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0o755
        results[provider] = f"installed → {shim_path}  (real: {real_bin})"

    # Only inject PATH if at least one shim was actually installed
    any_installed = any("installed" in msg for msg in results.values())
    if any_installed:
        profile = _get_shell_profile_path()
        if profile:
            modified = _inject_profile_block(profile)
            if modified:
                results["profile"] = f"PATH prepended in {profile}"
            else:
                results["profile"] = f"PATH block already present in {profile}"
        else:
            results["profile"] = "unknown shell — add ~/.longhouse/shims to PATH manually"
    else:
        results["profile"] = "skipped — no shims installed"

    return results


def uninstall_wrappers(providers: list[str] | None = None) -> dict[str, str]:
    """Remove wrapper shims.

    Returns a dict of ``{provider: status_message}``.
    """
    providers = _validate_providers(providers)
    results: dict[str, str] = {}

    shims_dir = _shims_dir()
    for provider in providers:
        shim_path = shims_dir / provider
        if shim_path.exists():
            shim_path.unlink()
            results[provider] = f"removed {shim_path}"
        else:
            results[provider] = "not installed"

    # Remove profile block only if no shims remain
    remaining_shims = list(shims_dir.glob("*")) if shims_dir.exists() else []
    if not remaining_shims:
        profile = _get_shell_profile_path()
        if profile:
            removed = _remove_profile_block(profile)
            results["profile"] = f"PATH block removed from {profile}" if removed else "no PATH block to remove"
        else:
            results["profile"] = "unknown shell — remove ~/.longhouse/shims from PATH manually"

        # Clean up empty dir
        if shims_dir.exists():
            try:
                shims_dir.rmdir()
            except OSError:
                pass
    else:
        remaining = [p.name for p in remaining_shims]
        results["profile"] = f"PATH block kept (remaining shims: {', '.join(remaining)})"

    return results


def get_wrapper_status() -> dict[str, dict[str, str | bool]]:
    """Return status info for each supported provider.

    Keys per provider:
        installed (bool), shim_path (str), real_binary (str | None)
    Also includes a ``profile`` key with PATH injection status.
    """
    shims_dir = _shims_dir()
    status: dict[str, dict[str, str | bool]] = {}

    for provider in SUPPORTED_PROVIDERS:
        shim_path = shims_dir / provider
        real_bin = _find_real_binary(provider)
        status[provider] = {
            "installed": shim_path.exists(),
            "shim_path": str(shim_path),
            "real_binary": real_bin or "not found",
        }

    profile = _get_shell_profile_path()
    if profile:
        has_block = _profile_has_block(profile)
        status["profile"] = {
            "installed": has_block,
            "path": str(profile),
            "shims_dir": str(shims_dir),
        }
    else:
        status["profile"] = {
            "installed": False,
            "path": "unknown shell",
            "shims_dir": str(shims_dir),
        }

    return status
