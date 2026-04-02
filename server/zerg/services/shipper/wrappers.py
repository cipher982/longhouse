"""CLI wrapper functions for managed-local sessions.

Injects shell functions into the user's profile that intercept bare
``claude`` / ``codex`` invocations and route interactive session launches
through ``longhouse claude`` / ``longhouse codex``.  Everything else
passes through to the real upstream binary via ``command``.

This replaces the previous PATH-shim approach with transparent shell
functions — easier to inspect (``type claude``), scoped to interactive
shells, and no ambient PATH mutation.

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
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("claude", "codex")

# Exit code used by ``longhouse claude`` / ``longhouse codex`` when
# Longhouse itself is unavailable (no config, API unreachable, auth
# failure).  The shell wrapper function checks for this code and falls
# back to the native CLI.
EXIT_SETUP_FAILED = 78

# Passthrough subcommands per provider.
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

# Flags that indicate non-interactive / pipe mode.
_NONINTERACTIVE_FLAGS = {"-p", "--pipe", "--print", "--print-session-id"}

# Per-provider markers in the shell profile.
_MARKER_BEGIN_FMT = "# >>> longhouse {provider} wrapper >>>"
_MARKER_END_FMT = "# <<< longhouse {provider} wrapper <<<"


def _marker_begin(provider: str) -> str:
    return _MARKER_BEGIN_FMT.format(provider=provider)


def _marker_end(provider: str) -> str:
    return _MARKER_END_FMT.format(provider=provider)


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


def _is_fish_shell() -> bool:
    return os.path.basename(os.environ.get("SHELL", "")) == "fish"


# ------------------------------------------------------------------
# Shell function generation
# ------------------------------------------------------------------


def _build_shell_function_posix(provider: str) -> str:
    """Build a bash/zsh function that wraps *provider*."""
    cmds = "|".join(sorted(_PASSTHROUGH_SUBCOMMANDS.get(provider, set())))
    flags_help = "--help|-h|--version|-v"
    ni_flags = "|".join(sorted(_NONINTERACTIVE_FLAGS))
    begin = _marker_begin(provider)
    end = _marker_end(provider)

    return f"""\
{begin}
{provider}() {{
    # Bypass
    if [ "${{LONGHOUSE_BYPASS:-0}}" = "1" ]; then command {provider} "$@"; return; fi
    # Passthrough subcommands & help/version flags
    case "${{1:-}}" in
        {cmds}|{flags_help}) command {provider} "$@"; return ;;
    esac
    # Passthrough non-interactive flags
    for _lh_a in "$@"; do
        case "$_lh_a" in {ni_flags}) command {provider} "$@"; return ;; esac
    done
    # Passthrough if not a TTY or any args present (v1: bare invocations only)
    if [ ! -t 0 ] || [ ! -t 1 ] || [ $# -gt 0 ]; then command {provider} "$@"; return; fi
    # Managed-local launch with fallback to native on setup failure
    longhouse {provider}
    _lh_rc=$?
    if [ "$_lh_rc" -eq {EXIT_SETUP_FAILED} ]; then
        echo "longhouse: managed launch unavailable, launching native {provider}" >&2
        command {provider}
    else
        return "$_lh_rc"
    fi
}}
{end}
"""


def _build_shell_function_fish(provider: str) -> str:
    """Build a fish function that wraps *provider*."""
    cmds = " ".join(sorted(_PASSTHROUGH_SUBCOMMANDS.get(provider, set())))
    ni_flags = " ".join(sorted(_NONINTERACTIVE_FLAGS))
    begin = _marker_begin(provider)
    end = _marker_end(provider)

    return f"""\
{begin}
function {provider}
    # Bypass
    if test "$LONGHOUSE_BYPASS" = "1"; command {provider} $argv; return; end
    # Passthrough subcommands & help/version flags
    switch "$argv[1]"
        case {cmds} --help -h --version -v
            command {provider} $argv; return
    end
    # Passthrough non-interactive flags
    for _lh_a in $argv
        switch "$_lh_a"
            case {ni_flags}
                command {provider} $argv; return
        end
    end
    # Passthrough if not a TTY or any args present
    if not isatty stdin; or not isatty stdout; or test (count $argv) -gt 0
        command {provider} $argv; return
    end
    # Managed-local launch with fallback
    longhouse {provider}
    set _lh_rc $status
    if test "$_lh_rc" -eq {EXIT_SETUP_FAILED}
        echo "longhouse: managed launch unavailable, launching native {provider}" >&2
        command {provider}
    else
        return "$_lh_rc"
    end
end
{end}
"""


def build_shell_function(provider: str) -> str:
    """Return the appropriate shell function for the user's shell."""
    if _is_fish_shell():
        return _build_shell_function_fish(provider)
    return _build_shell_function_posix(provider)


# ------------------------------------------------------------------
# Profile block management (per-provider)
# ------------------------------------------------------------------


def _profile_has_block(profile_path: Path, provider: str) -> bool:
    if not profile_path.exists():
        return False
    text = profile_path.read_text()
    return _marker_begin(provider) in text and _marker_end(provider) in text


def _inject_profile_block(profile_path: Path, provider: str) -> bool:
    """Append a wrapper function block for *provider*.  Returns True if modified."""
    if _profile_has_block(profile_path, provider):
        return False

    block = build_shell_function(provider)

    existing = ""
    if profile_path.exists():
        existing = profile_path.read_text()
    separator = "" if existing.endswith("\n") or not existing else "\n"

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("a") as f:
        f.write(f"{separator}\n{block}")
    return True


def _remove_profile_block(profile_path: Path, provider: str) -> bool:
    """Remove the wrapper function block for *provider*.  Returns True if modified."""
    if not profile_path.exists():
        return False
    text = profile_path.read_text()
    begin = _marker_begin(provider)
    end = _marker_end(provider)
    if begin not in text:
        return False

    pattern = rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n?"
    cleaned = re.sub(pattern, "\n", text, flags=re.DOTALL)
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
    """Find the upstream binary on PATH."""
    return shutil.which(provider)


def install_wrappers(providers: list[str] | None = None) -> dict[str, str]:
    """Install wrapper shell functions into the user's profile.

    Returns a dict of ``{provider: status_message}``.
    """
    providers = _validate_providers(providers)
    results: dict[str, str] = {}

    profile = _get_shell_profile_path()

    for provider in providers:
        real_bin = _find_real_binary(provider)
        if not real_bin:
            results[provider] = f"skipped — '{provider}' binary not found on PATH"
            continue

        if not profile:
            results[provider] = "skipped — unknown shell (add function manually)"
            continue

        modified = _inject_profile_block(profile, provider)
        if modified:
            results[provider] = f"installed in {profile}  (real: {real_bin})"
        else:
            results[provider] = f"already installed in {profile}"

    return results


def uninstall_wrappers(providers: list[str] | None = None) -> dict[str, str]:
    """Remove wrapper shell functions from the user's profile.

    Returns a dict of ``{provider: status_message}``.
    """
    providers = _validate_providers(providers)
    results: dict[str, str] = {}

    profile = _get_shell_profile_path()

    for provider in providers:
        if not profile:
            results[provider] = "skipped — unknown shell (remove function manually)"
            continue

        removed = _remove_profile_block(profile, provider)
        if removed:
            results[provider] = f"removed from {profile}"
        else:
            results[provider] = "not installed"

    return results


def get_wrapper_status() -> dict[str, dict[str, str | bool]]:
    """Return status info for each supported provider.

    Keys per provider: ``installed``, ``real_binary``.
    Also includes a ``profile`` key with the shell profile path.
    """
    profile = _get_shell_profile_path()
    status: dict[str, dict[str, str | bool]] = {}

    for provider in SUPPORTED_PROVIDERS:
        real_bin = _find_real_binary(provider)
        installed = bool(profile and _profile_has_block(profile, provider))
        status[provider] = {
            "installed": installed,
            "real_binary": real_bin or "not found",
        }

    status["profile"] = {
        "installed": any(status[p]["installed"] for p in SUPPORTED_PROVIDERS),
        "path": str(profile) if profile else "unknown shell",
    }

    return status
