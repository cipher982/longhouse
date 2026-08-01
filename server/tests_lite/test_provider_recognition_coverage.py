"""Every launch-tier provider must be recognizable everywhere it is observed.

Two hand-written provider lists silently excluded Cursor for a month:

- `local_health/process.py` `_provider_for_cmdline` had no cursor branch, so no
  running `cursor-agent` -- managed Helm or bare Shadow -- ever appeared in
  local-health process evidence, which is what `longhouse doctor` uses to
  reconcile what is running against what Longhouse believes.
- `session_turns.py` filtered managed turns on a control-plane tuple that
  predated Cursor, so every Cursor Helm managed turn was missing from
  `/api/observability/*` and `/api/agents/turns`.

Both now derive from the provider contract. These tests keep them derived.
"""

from __future__ import annotations

import pytest

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.services.local_health.process import _provider_for_cmdline
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import factory_provider_names
from zerg.services.session_turns import _managed_turn_control_planes


@pytest.mark.parametrize("provider", sorted(factory_provider_names(include_maintenance=True)))
def test_every_provider_binary_is_recognized_by_the_process_scan(provider: str) -> None:
    """A provider Longhouse can launch is a provider it must be able to see."""

    binary = PROVIDER_CLI_BINARY_BY_PROVIDER[provider]
    assert _provider_for_cmdline([f"/usr/local/bin/{binary}"]) == provider
    assert _provider_for_cmdline([binary, "--resume", "abc"]) == provider


def test_longhouse_own_binaries_are_never_claimed_as_a_provider() -> None:
    """The wrapper matchers exclude `longhouse-` scripts; the derived fallback must too."""

    for cmdline in (
        ["longhouse-engine", "cursor-helm", "launch"],
        ["/opt/bin/longhouse-codex"],
        ["node", "/x/longhouse-opencode.js"],
    ):
        assert _provider_for_cmdline(cmdline) is None


def test_wrapper_shapes_still_resolve() -> None:
    """The derived fallback must not have replaced the wrapper-shape knowledge."""

    assert _provider_for_cmdline(["node", "/opt/opencode/opencode.js"]) == "opencode"
    assert _provider_for_cmdline(["bun", "/opt/agy/agy.js"]) == "antigravity"
    assert _provider_for_cmdline(["/x/codex-darwin-arm64"]) == "codex"


def test_managed_turn_control_planes_cover_every_contract_plane() -> None:
    """A first-tier provider cannot be invisible to managed-turn observability."""

    planes = _managed_turn_control_planes()
    for contract in all_managed_provider_contracts():
        for plane in contract.control_planes:
            assert plane in planes, (
                f"{contract.provider}'s control plane {plane} is missing from the managed-turn "
                "filter, so its turns would not reach /api/observability or /api/agents/turns"
            )


def test_managed_turn_control_planes_keep_the_legacy_transport_planes() -> None:
    """Named policy, not derivation -- assert it stays deliberate."""

    planes = _managed_turn_control_planes()
    assert "opencode_process" in planes
    assert "antigravity_process" in planes
