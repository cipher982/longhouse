"""Built-in Channel Plugins.

This package contains the built-in channel implementations for Zerg.
Third-party plugins can be loaded dynamically via the registry.
"""

from zerg.channels.plugins.mock import MockChannel

__all__ = ["MockChannel"]
