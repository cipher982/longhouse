"""The harness raises tight per-call RPC budgets; production numbers stay put.

Call sites pass literals like ``timeout_seconds=1.0`` that suit a long-lived
daemon on its own host. Under the harness the daemon started milliseconds ago
and shares a machine with the rest of the suite, so on a loaded CI runner a
healthy call exceeds one second and the route reports the timeout as
"unavailable" -- which is how two tests failed in CI while passing locally.
"""

from __future__ import annotations

from zerg.catalogd.client import CatalogClient

from tests_lite.live_catalog_harness import RPC_TIMEOUT_SECONDS
from tests_lite.live_catalog_harness import floored_rpc_timeout
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401


def test_a_tighter_budget_is_raised_to_the_floor():
    assert floored_rpc_timeout(1.0) == RPC_TIMEOUT_SECONDS
    assert floored_rpc_timeout(0.25) == RPC_TIMEOUT_SECONDS


def test_none_and_generous_budgets_are_left_alone():
    # None means "use the client's own default"; the floor must not invent one.
    assert floored_rpc_timeout(None) is None
    assert floored_rpc_timeout(RPC_TIMEOUT_SECONDS) == RPC_TIMEOUT_SECONDS
    assert floored_rpc_timeout(120.0) == 120.0


def test_the_floor_is_actually_installed_on_the_client(live_catalog):  # noqa: F811
    # Guards the wiring, not just the arithmetic: a provisioned catalog must
    # have replaced CatalogClient.call, or the floor above is dead code.
    assert CatalogClient.call.__name__ == "_floored"
