"""Every catalogd RPC method answers a malformed request exactly as recorded.

The golden file was captured from the hand-written ``_dispatch`` chain before it
became a method table (the ``declared_nulls`` case from that chain with each
handler's own key set); regenerate it only for an intended wire change:
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
    yield root
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


async def _run(root: Path, name: str, requests: list[tuple[str, dict]]) -> list[dict]:
    daemon = CatalogDaemon(database_path=root / f"{name}.db", socket_path=root / f"{name}.sock")
    await daemon.start()
    await daemon._projector_repair_task
    outcomes = []
    try:
        for method, params in requests:
            request = CatalogRpcRequest(
                id=uuid4().hex,
                method=method,
                deadline_mono_ns=str(time.monotonic_ns() + 30_000_000_000),
                params=params,
            )
            try:
                outcomes.append(_outcome(await daemon._dispatch(request)))
            except Exception as exc:  # recorded, not swallowed: it is part of the contract
                outcomes.append({"raised": type(exc).__name__})
    finally:
        await daemon.close()
    return outcomes


@pytest.mark.asyncio
async def test_every_method_answers_malformed_and_null_params_exactly_as_recorded(daemon_paths, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    golden = json.loads(GOLDEN.read_text())
    observed: dict[str, dict] = {method: {} for method in golden}

    # A route that declares its keys must still reach its handler with exactly
    # those keys: null values fail (or pass) the handler's own value checks.
    declared = sorted(method for method, route in CatalogDaemon._METHODS.items() if route.params is not None)
    nulls = await _run(daemon_paths, "nulls", [(m, dict.fromkeys(CatalogDaemon._METHODS[m].params)) for m in declared])
    for method, outcome in zip(declared, nulls, strict=True):
        observed.setdefault(method, {})["declared_nulls"] = outcome

    methods = sorted(set(golden) - set(_LAST)) + list(_LAST)
    requests = [(method, dict(params)) for method in methods for params in _CASES.values()]
    malformed = iter(await _run(daemon_paths, "malformed", requests))
    for method in methods:
        for case in _CASES:
            observed[method][case] = next(malformed)

    if os.getenv("LONGHOUSE_WRITE_DISPATCH_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(observed, indent=1, sort_keys=True) + "\n")
        pytest.skip("golden rewritten; rerun without LONGHOUSE_WRITE_DISPATCH_GOLDEN")
    assert observed == golden


def test_golden_covers_every_routed_method_and_handler_exists():
    golden = json.loads(GOLDEN.read_text())
    routed = set(CatalogDaemon._METHODS) | set(CatalogDaemon._INLINE_METHODS)
    assert set(golden) - set(_UNKNOWN) == routed
    for method, route in CatalogDaemon._METHODS.items():
        assert callable(getattr(CatalogDaemon, route.handler, None)), method


@pytest.mark.asyncio
async def test_e2e_reset_route_rejects_params_only_when_enabled(daemon_paths, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test:e2e")
    monkeypatch.setenv("TESTING", "1")
    extra, empty = await _run(daemon_paths, "reset", [("test.user_data.reset.v2", {"x": 1}), ("test.user_data.reset.v2", {})])
    assert extra["code"] == "invalid_request"
    assert extra["message"] == "test.user_data.reset.v2 accepts no parameters"
    assert empty == {"ok": True}
