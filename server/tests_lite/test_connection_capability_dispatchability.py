"""A connection capability may not advertise a control the dispatcher refuses.

`SessionConnection.can_send_input` / `can_interrupt` / `can_terminate` are what
web and iOS read (through the session-state projection) to decide whether to
offer a control. They are written at launch from
`ManagedProviderContract.connection_capabilities`.

Until 2026-07-31 those three came straight off the schema's operation flags,
while the dispatcher gates on `machine_control_supports` from the *same schema
file*. Claude and Codex declare `terminate: true` and carry no
`claude.terminate` / `codex.terminate` support, so every managed Claude and
Codex session advertised a terminate control that
`managed_control_dispatcher._session_uses_engine_control` refused before the
engine was contacted — and which `control_channel.rs` COMMAND_TERMINATE does
not implement for those providers either. Absent at three layers, advertised at
the fourth.

That is the same shape as the `longhouse cursor` coordination outage: a
declaration the executing code does not honor. These tests force the two
statements in the schema to agree with each other and with the dispatcher.
"""

from __future__ import annotations

import pytest

from zerg.managed_provider_contract_manifest import MACHINE_CONTROL_SUPPORT_OPERATION_BY_SUFFIX
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import machine_control_capability_for_command

# connection capability column -> (contract operation, dispatcher command_type)
_REMOTE_CONTROL_COLUMNS = {
    "can_send_input": ("send_input", "session.send_text"),
    "can_interrupt": ("interrupt", "session.interrupt"),
    "can_terminate": ("terminate", "session.terminate"),
}


@pytest.mark.parametrize("column", sorted(_REMOTE_CONTROL_COLUMNS))
def test_advertised_connection_capability_is_dispatchable(column: str) -> None:
    """can_* is a promise to a client. Only make it when the command can be sent."""

    operation, command_type = _REMOTE_CONTROL_COLUMNS[column]
    for contract in all_managed_provider_contracts():
        advertised = bool(contract.connection_capabilities[column])
        if not advertised:
            continue
        assert contract.supports_contract_operation(operation), (
            f"{contract.provider} advertises {column} but the contract's {operation} flag is false"
        )
        assert machine_control_capability_for_command(contract.provider, command_type) is not None, (
            f"{contract.provider} advertises {column}, but no machine-control support maps to "
            f"{command_type}. A client would offer this control and the dispatcher would refuse it. "
            f"Either add {contract.provider}.<op> to machine_control_supports and implement the "
            f"engine dispatch path, or the operation flag is wrong."
        )


def test_dispatchable_remote_control_is_advertised() -> None:
    """The other direction: shipping a dispatch path nothing surfaces is waste."""

    for contract in all_managed_provider_contracts():
        for column, (_operation, command_type) in _REMOTE_CONTROL_COLUMNS.items():
            if machine_control_capability_for_command(contract.provider, command_type) is None:
                continue
            assert contract.connection_capabilities[column] == 1, (
                f"{contract.provider} can dispatch {command_type} but does not advertise {column}"
            )


def test_terminate_is_honest_for_every_provider() -> None:
    """Pin the specific regression, by name, so it cannot silently return.

    Cursor and OpenCode carry a real remote terminate. Claude and Codex do not:
    `machine_control_supports` omits it and `control_channel.rs`
    COMMAND_TERMINATE implements only opencode and cursor. Whichever way that
    product decision goes, the advertisement must follow the implementation.
    """

    by_provider = {c.provider: c for c in all_managed_provider_contracts()}
    for provider, contract in by_provider.items():
        advertised = bool(contract.connection_capabilities["can_terminate"])
        dispatchable = machine_control_capability_for_command(provider, "session.terminate") is not None
        assert advertised == dispatchable, (
            f"{provider}: can_terminate={advertised} but dispatchable={dispatchable}. "
            "If remote terminate was implemented for this provider, add its support entry; "
            "if it was removed, the advertisement must go with it."
        )


def test_every_machine_control_support_maps_to_a_known_operation() -> None:
    """A support string whose suffix is unknown silently advertises nothing."""

    for contract in all_managed_provider_contracts():
        for support in contract.machine_control_supports:
            provider, _, suffix = support.partition(".")
            assert provider == contract.provider, f"{support} is declared under {contract.provider}"
            assert suffix in MACHINE_CONTROL_SUPPORT_OPERATION_BY_SUFFIX, (
                f"{support} has no entry in MACHINE_CONTROL_SUPPORT_OPERATION_BY_SUFFIX, so it "
                "advertises a capability nothing can resolve"
            )
