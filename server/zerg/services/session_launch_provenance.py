"""Shared launch provenance vocabulary for user-owned session starts."""

from __future__ import annotations

from collections.abc import Mapping

LAUNCH_ACTOR_HUMAN_SHELL = "human_shell"
LAUNCH_ACTOR_HUMAN_UI = "human_ui"
LAUNCH_ACTOR_AUTOMATION = "automation"

LAUNCH_SURFACE_TERMINAL = "terminal"
LAUNCH_SURFACE_WEB = "web"
LAUNCH_SURFACE_IOS = "ios"
LAUNCH_SURFACE_API = "api"
LAUNCH_SURFACE_HATCH = "hatch"
LAUNCH_SURFACE_TEST = "test"
LAUNCH_SURFACE_CI = "ci"
LAUNCH_SURFACE_PROVIDER_SUBPROCESS = "provider_subprocess"

VALID_LAUNCH_ACTORS = frozenset(
    {
        LAUNCH_ACTOR_HUMAN_SHELL,
        LAUNCH_ACTOR_HUMAN_UI,
        LAUNCH_ACTOR_AUTOMATION,
    }
)
VALID_LAUNCH_SURFACES = frozenset(
    {
        LAUNCH_SURFACE_TERMINAL,
        LAUNCH_SURFACE_WEB,
        LAUNCH_SURFACE_IOS,
        LAUNCH_SURFACE_API,
        LAUNCH_SURFACE_HATCH,
        LAUNCH_SURFACE_TEST,
        LAUNCH_SURFACE_CI,
        LAUNCH_SURFACE_PROVIDER_SUBPROCESS,
    }
)
HUMAN_LAUNCH_ACTORS = frozenset({LAUNCH_ACTOR_HUMAN_SHELL, LAUNCH_ACTOR_HUMAN_UI})
HIDDEN_FROM_DEFAULT_ORIGIN_KINDS = frozenset({"hatch_automation", "test_or_canary"})


def _normalize_token(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized or None


def normalize_launch_actor(value: str | None) -> str | None:
    normalized = _normalize_token(value)
    if normalized in VALID_LAUNCH_ACTORS:
        return normalized
    return None


def normalize_launch_surface(value: str | None) -> str | None:
    normalized = _normalize_token(value)
    if normalized in VALID_LAUNCH_SURFACES:
        return normalized
    return None


def normalize_hidden_origin_kind(value: str | None) -> str | None:
    normalized = _normalize_token(value)
    if normalized in HIDDEN_FROM_DEFAULT_ORIGIN_KINDS:
        return normalized
    return None


def hidden_origin_blocks_launch_actor(origin_kind: str | None, launch_actor: str | None) -> bool:
    return origin_kind in HIDDEN_FROM_DEFAULT_ORIGIN_KINDS and launch_actor in HUMAN_LAUNCH_ACTORS


def sanitize_launch_provenance(
    *,
    origin_kind: str | None,
    launch_actor: str | None,
    launch_surface: str | None,
) -> tuple[str | None, str | None]:
    """Normalize launch provenance and drop inherited human stamps from hidden automation."""

    actor = normalize_launch_actor(launch_actor)
    surface = normalize_launch_surface(launch_surface)
    hidden_origin = normalize_hidden_origin_kind(origin_kind)
    if actor is None:
        return None, None
    if hidden_origin_blocks_launch_actor(hidden_origin, actor):
        return None, None
    return actor, surface


def human_shell_provenance_for_interactive_tty(
    *,
    env: Mapping[str, str | None],
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> tuple[str | None, str | None]:
    """Return the terminal-human stamp only for direct interactive wrapper launches."""

    if not stdin_is_tty or not stdout_is_tty:
        return None, None
    if normalize_hidden_origin_kind(env.get("LONGHOUSE_ORIGIN_KIND")):
        return None, None
    sidechain = str(env.get("LONGHOUSE_IS_SIDECHAIN") or "").strip().lower()
    if sidechain in {"1", "true", "yes", "on"}:
        return None, None
    return LAUNCH_ACTOR_HUMAN_SHELL, LAUNCH_SURFACE_TERMINAL
