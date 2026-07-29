"""Per-provider UniversalProviderAdapter subclasses, one module each.

Phase 3 of docs/specs/provider-factory-coherence.md ("split the adapter").
Importing this package is what makes registration real: each submodule
carries a `@register_adapter(provider)` decorator (defined in
universal_agent_harness.py) that only runs when the module is imported.
Review 2026-07-29 (Fable) named this exact gap -- a package split alone
does not wire discover_adapters() into anything; something at import time
still has to import every provider module, or ADAPTER_CLASS_BY_PROVIDER
stays empty for any provider whose module nobody imported.

`load_all()` is that forcing function -- called from
universal_agent_harness.adapter_registry(), the real production entry
point, so every caller of that function gets real registration without
needing to know this package exists.

All five provider adapters live here. `load_all()` discovers modules from the
package instead of naming providers, so a sixth adapter becomes available to
the production registry by adding its module; this file does not need another
provider-specific edit.
"""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules


def load_all() -> None:
    """Import every provider adapter module, registering it as a side effect."""
    for module in sorted(iter_modules(__path__), key=lambda candidate: candidate.name):
        if not module.name.startswith("_"):
            import_module(f"{__name__}.{module.name}")
