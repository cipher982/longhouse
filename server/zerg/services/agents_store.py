"""Compatibility shim for imports that still use ``zerg.services.agents_store``."""

from zerg.services.agents import *  # noqa: F401, F403

__doc__ = (
    "Compatibility shim for imports that still use " "`zerg.services.agents_store`; real implementation lives in " "`zerg.services.agents`."
)
