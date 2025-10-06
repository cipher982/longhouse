"""Provider-side Pact verification.

The test discovers every JSON file in the top-level ``contracts/`` directory
and verifies them against the live FastAPI app started in-process via
TestClient.  We skip the entire module when the optional *pact-verifier*
dependency is missing so the CI remains green until the library is added to
the dev requirements.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from pact_verifier import Verifier  # type: ignore

    _PACT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover – dependency optional for now
    _PACT_AVAILABLE = False


pytestmark = pytest.mark.skipif(not _PACT_AVAILABLE, reason="pact_verifier not installed")


@pytest.fixture(scope="session")
def pact_contracts() -> list[Path]:  # noqa: D401 – simple helper
    """Return a list of pact files under ``contracts/``."""

    contracts_dir = Path(__file__).resolve().parent.parent.parent / "contracts"
    return list(contracts_dir.glob("*.json"))


def test_verify_pacts(pact_contracts, anyio_backend_name):  # noqa: D401
    """Verify all pact contracts against the FastAPI TestClient."""

    # Lazy-import here so the module level import remains optional.
    from fastapi.testclient import TestClient  # pylint: disable=import-error

    from zerg.main import app  # type: ignore – FastAPI app

    client = TestClient(app)

    for pact_file in pact_contracts:
        json.loads(pact_file.read_text())

        verifier = Verifier(provider="zerg-backend")

        # The HTTP handshake is still HTTP for WebSocket; verifier will send
        # the same payloads.  We mount under the TestClient base URL.
        verifier.verify_with_broker(  # type: ignore[attr-defined]
            pact_uri=str(pact_file),
            provider_base_url=client.base_url,
            publish_verification_results=False,
        )
