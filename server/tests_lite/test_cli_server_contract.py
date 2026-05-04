"""CLI ↔ server schema contract tests.

Catches drift between the CLI's payload builders and the server's Pydantic
request models. Without these, a CLI installed before a server enum change
(e.g. loop_mode {manual,assist,autopilot} → {assist,autopilot}) will only
surface the error when a human runs `lho` and reads a 422 traceback.

Strategy: import the CLI's actual payload-building function, pass it
defaults the real CLI would use, and validate the resulting dict against
the server's Pydantic request model directly. No network, no TestClient —
the Pydantic model is the single source of truth for the wire shape.
"""

from pathlib import Path

import pytest

from zerg.cli.claude import build_managed_local_launch_payload
from zerg.routers.session_chat import ManagedLocalThisDeviceLaunchRequest
from zerg.session_loop_mode import SessionLoopMode


def _cwd_tmp(tmp_path: Path) -> Path:
    # Subdir so git context inference doesn't crash on a non-repo tmp root.
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("loop_mode", list(SessionLoopMode))
def test_managed_local_launch_payload_matches_server_schema(tmp_path, provider, loop_mode):
    """Every CLI-defaulted payload must validate against the server model.

    Guards against enum drift, renamed fields, and required-field removal.
    """
    payload = build_managed_local_launch_payload(
        cwd=_cwd_tmp(tmp_path),
        provider=provider,
        project=None,
        name=None,
        loop_mode=loop_mode,
        machine_name="test-host",
        native_claude_channels_available=True if provider == "claude" else None,
        claude_launch_env=None,
    )

    # Will raise ValidationError if any field is the wrong type / missing /
    # uses a value the server no longer accepts (e.g. a removed enum variant).
    ManagedLocalThisDeviceLaunchRequest(**payload)


def test_cli_default_loop_mode_is_accepted_by_server(tmp_path):
    """The CLI's default loop mode must be one the server still accepts.

    Belt-and-braces guard for the specific drift that broke `lho` earlier:
    CLI defaulted to 'manual' after server dropped that enum variant.
    """
    from zerg.cli.codex import codex as _codex_signature_holder  # noqa: F401

    # Typer Option defaults live in the function signature. Import and
    # inspect the default value on the loop_mode parameter directly.
    import inspect

    from zerg.cli import codex as codex_cli

    params = inspect.signature(codex_cli.codex).parameters
    loop_mode_default = params["loop_mode"].default
    # Typer wraps defaults in OptionInfo; pull the underlying value.
    raw_default = getattr(loop_mode_default, "default", loop_mode_default)

    payload = build_managed_local_launch_payload(
        cwd=_cwd_tmp(tmp_path),
        provider="codex",
        project=None,
        name=None,
        loop_mode=raw_default,
        machine_name="test-host",
    )
    ManagedLocalThisDeviceLaunchRequest(**payload)


def test_removed_loop_mode_value_is_rejected(tmp_path):
    """Sanity: if someone re-adds a stale variant to the CLI it should fail.

    This is a regression trap for the exact drift we hit.
    """
    payload = build_managed_local_launch_payload(
        cwd=_cwd_tmp(tmp_path),
        provider="codex",
        project=None,
        name=None,
        loop_mode=SessionLoopMode.ASSIST,
        machine_name="test-host",
    )
    payload["loop_mode"] = "manual"  # stale value the server no longer accepts
    with pytest.raises(Exception):
        ManagedLocalThisDeviceLaunchRequest(**payload)
