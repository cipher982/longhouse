from __future__ import annotations

from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import register_adapter


@register_adapter("cursor")
class CursorHarnessAdapter(UniversalProviderAdapter):
    """Cursor concrete adapter for the universal Longhouse action contract."""
