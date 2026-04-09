"""Compatibility shim - agents_store module has been split into the agents package.

For backward compatibility, all symbols are re-exported from the new location.
New code should import from zerg.services.agents directly.
"""

# Re-export all public symbols from the agents package
from zerg.services.agents import *  # noqa: F401, F403

__doc__ = """Agents store service for session and event CRUD operations.

Provides a clean interface for ingesting and querying AI coding sessions
from any provider (Claude Code, Codex, Gemini, Cursor, Oikos).

This module is a compatibility shim. The actual implementation has been
modularized into the agents package.
"""
