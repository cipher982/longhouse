"""Public exports for the agents service package.

This package is imported by both the server (which has ``DATABASE_URL``
configured) and by remote-only CLI launchers such as ``longhouse cursor``
(which do NOT). The heavy submodules — ``store`` (``AgentsStore``),
``schema``, ``helpers`` and ``identity_resolver`` — transitively import
``zerg.database``, which validates required config (including
``DATABASE_URL``) at import time. Eagerly importing them here would crash
every remote-only CLI that touches this package.

To stay importable in both environments, the package init is lazy: only the
light Pydantic wire-contract submodule (``models``) is safe to import
without a DB, and even it is loaded on demand. Every public name is resolved
through ``__getattr__`` (PEP 562), so a caller that only needs
``SessionIngest`` never pays for ``AgentsStore`` / DB initialization.
"""

from __future__ import annotations

# Maps each public name to the submodule that defines it. ``__getattr__``
# resolves on demand so the package init never eagerly pulls in DB-bound code.
_LAZY: dict[str, str] = {
    # models — pure Pydantic wire contracts (DB-free at import time)
    "CompactionBoundary": "models",
    "EventIngest": "models",
    "IngestResult": "models",
    "RewindSignal": "models",
    "SessionIngest": "models",
    "SessionProjectionItem": "models",
    "SessionProjectionPage": "models",
    "SourceLineIngest": "models",
    "SourceRewindHintIngest": "models",
    # helpers — DB-bound (imports zerg.models.agents)
    "_infer_execution_home_from_ingest": "helpers",
    "_infer_origin_label_from_ingest": "helpers",
    "_normalize_utc_naive": "helpers",
    "_should_replace_managed_local_placeholder_provider_session_id": "helpers",
    # identity_resolver — DB-bound
    "ObservedActor": "identity_resolver",
    "ObservedCapability": "identity_resolver",
    "ObservedLineageEdge": "identity_resolver",
    "ObservedRun": "identity_resolver",
    "ObservedSession": "identity_resolver",
    "SessionProjectionDecision": "identity_resolver",
    "classify_lineage_kind": "identity_resolver",
    "observed_lineage_from_evidence": "identity_resolver",
    "resolve_session_projection": "identity_resolver",
    # schema — DB-bound
    "ensure_agents_schema": "schema",
    # store — DB-bound
    "AgentsStore": "store",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    sub = _LAZY.get(name)
    if sub is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod = importlib.import_module(f"{__name__}.{sub}")
    try:
        value = getattr(mod, name)
    except AttributeError as exc:  # pragma: no cover - contract drift guard
        raise ImportError(f"{__name__}.{sub} does not export {name!r}") from exc
    # Cache on this module so subsequent lookups skip the import machinery.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY))
