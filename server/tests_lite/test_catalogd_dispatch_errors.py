"""Every catalogd RPC method answers a malformed request exactly as recorded.

The golden file was captured from the hand-written ``_dispatch`` chain before it
became a method table; regenerate it only for an intended wire change:
``LONGHOUSE_WRITE_DISPATCH_GOLDEN=1 uv run pytest tests_lite/test_catalogd_dispatch_errors.py``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.server import CatalogDaemon

GOLDEN = Path(__file__).parent / "fixtures" / "catalogd_dispatch_errors.json"

# Requests every method must reject or accept identically before and after a
# dispatch refactor. The writer admission toggles run last, close before open,
# so the store state the other methods see does not depend on them.
_CASES = {"extra": {"__unexpected__": 1}, "empty": {}}
_LAST = ("writer.admission.close.v2", "writer.admission.open.v2")
_UNKNOWN = ("nonexistent.method.v2", "test.user_data.reset.v2")


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _outcome(response) -> dict:
    if response.error is None:
        return {"ok": True}
    error = response.error
    return {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "retry_after_ms": error.retry_after_ms,
        "details": error.details,
    }


@pytest.mark.asyncio
async def test_every_method_rejects_malformed_params_exactly_as_recorded(daemon_paths, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    database_path, socket_path = daemon_paths
    golden = json.loads(GOLDEN.read_text())
    methods = sorted(set(golden) - set(_LAST)) + list(_LAST)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    await daemon._projector_repair_task
    observed: dict[str, dict] = {}
    try:
        for method in methods:
            observed[method] = {}
            for case, params in _CASES.items():
                request = CatalogRpcRequest(
                    id=uuid4().hex,
                    method=method,
                    deadline_mono_ns=str(time.monotonic_ns() + 30_000_000_000),
                    params=dict(params),
                )
                try:
                    observed[method][case] = _outcome(await daemon._dispatch(request))
                except Exception as exc:  # recorded, not swallowed: it is part of the contract
                    observed[method][case] = {"raised": type(exc).__name__}
    finally:
        await daemon.close()
    if os.getenv("LONGHOUSE_WRITE_DISPATCH_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(observed, indent=1, sort_keys=True) + "\n")
    assert observed == golden
