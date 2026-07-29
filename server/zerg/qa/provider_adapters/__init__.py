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

Providers extracted so far: cursor. The remaining four
(claude, codex, opencode, antigravity) still live in
universal_agent_harness.py -- same pattern, not yet moved. Each one is real,
separate work: resolving every name the moved class's methods reference
(module-level helpers, EvidencePackage/HarnessOptions types, shared private
methods) per provider, not a mechanical bulk cut.
"""

from __future__ import annotations


def load_all() -> None:
    """Import every provider adapter module, registering it as a side effect."""
    from zerg.qa.provider_adapters import cursor  # noqa: F401
