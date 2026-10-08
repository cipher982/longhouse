"""Patch a catalog store module-level name everywhere CatalogStore reads it.

CatalogStore's methods live in ``zerg.catalogd.store_mixins.*``, and each of
those modules binds the store helpers it calls, so patching only
``zerg.catalogd.store`` would miss methods that moved. This patches the name in
the store module and in every mixin module bound to the same object.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import pytest

import zerg.catalogd.store as store
import zerg.catalogd.store_mixins as store_mixins


def patch_store_global(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    original = getattr(store, name)
    modules = [store] + [
        importlib.import_module(f"{store_mixins.__name__}.{info.name}") for info in pkgutil.iter_modules(store_mixins.__path__)
    ]
    for module in modules:
        if getattr(module, name, None) is original:
            monkeypatch.setattr(module, name, value)
