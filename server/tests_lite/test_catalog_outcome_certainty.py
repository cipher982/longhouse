"""A lost answer is not proof that nothing happened.

`CatalogUnavailable` was raised identically for a socket error, where the
daemon never saw the request, and for a deadline expiry, where the write may
have committed and only the answer was lost. Callers mapped both to "this did
not happen". That lie killed a managed launch whose durable row already said
`adopted`, and it invites a resend of a directed input that was in fact
delivered — under a fresh client_request_id, which defeats the catalog's own
dedupe and reaches the peer twice.
"""

import asyncio

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogUnavailable


def test_socket_failure_reports_a_definite_non_event(tmp_path):
    """Nothing was sent, so a caller may safely say the effect did not happen."""

    client = CatalogClient(tmp_path / "missing.sock")

    with pytest.raises(CatalogUnavailable) as caught:
        asyncio.run(client.call("ping.v2", {}))

    assert caught.value.outcome_unknown is False


def test_deadline_expiry_reports_an_unknown_outcome():
    """The daemon may have committed and only the answer been lost."""

    error = CatalogUnavailable("catalogd deadline exceeded for x.v2", outcome_unknown=True)

    assert error.outcome_unknown is True


def test_default_is_the_safe_definite_case():
    assert CatalogUnavailable("boom").outcome_unknown is False
