"""Channel Plugin Registry.

Provides dynamic registration and discovery of channel plugins.
Supports both built-in channels and third-party plugins.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any
from typing import TypeVar

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.types import ChannelMeta

logger = logging.getLogger(__name__)


T = TypeVar("T", bound=ChannelPlugin)


class ChannelRegistryError(Exception):
    """Error during channel registration or lookup."""

    pass


class ChannelRegistry:
    """Registry for channel plugin discovery and management.

    The registry maintains a collection of available channel plugins
    and handles their lifecycle (configuration, startup, shutdown).

    Usage:
        registry = ChannelRegistry()
        registry.register(TelegramChannel())

        # Get a channel
        telegram = registry.get("telegram")

        # List all channels
        for channel in registry.list():
            print(channel.meta["name"])
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._channels: dict[str, ChannelPlugin] = {}
        self._aliases: dict[str, str] = {}
        self._loaded_plugins: set[str] = set()

    def register(
        self,
        channel: ChannelPlugin,
        *,
        replace: bool = False,
    ) -> None:
        """Register a channel plugin.

        Args:
            channel: The channel plugin instance to register
            replace: If True, replace existing channel with same ID

        Raises:
            ChannelRegistryError: If channel ID already exists and replace=False
        """
        channel_id = channel.meta.get("id", "")
        if not channel_id:
            raise ChannelRegistryError("Channel must have an 'id' in meta")

        if channel_id in self._channels and not replace:
            raise ChannelRegistryError(f"Channel '{channel_id}' is already registered. " f"Use replace=True to override.")

        self._channels[channel_id] = channel

        # Register aliases
        for alias in channel.meta.get("aliases", []):
            self._aliases[alias.lower()] = channel_id

        logger.info(f"Registered channel: {channel.meta.get('name', channel_id)} ({channel_id})")

    def unregister(self, channel_id: str) -> bool:
        """Unregister a channel plugin.

        Args:
            channel_id: ID of channel to unregister

        Returns:
            True if channel was unregistered, False if not found
        """
        channel = self._channels.pop(channel_id, None)
        if channel:
            # Remove aliases
            for alias in channel.meta.get("aliases", []):
                self._aliases.pop(alias.lower(), None)
            logger.info(f"Unregistered channel: {channel_id}")
            return True
        return False

    def get(self, channel_id: str) -> ChannelPlugin | None:
        """Get a registered channel by ID or alias.

        Args:
            channel_id: Channel ID or alias

        Returns:
            ChannelPlugin if found, None otherwise
        """
        normalized = channel_id.lower().strip()

        # Check direct ID match
        if normalized in self._channels:
            return self._channels[normalized]

        # Check aliases
        resolved_id = self._aliases.get(normalized)
        if resolved_id:
            return self._channels.get(resolved_id)

        return None

    def list(self) -> list[ChannelPlugin]:
        """List all registered channels.

        Returns:
            List of channel plugins sorted by display order
        """
        channels = list(self._channels.values())
        # Sort by order field, then by name
        return sorted(
            channels,
            key=lambda c: (c.meta.get("order", 999), c.meta.get("name", "")),
        )

    def list_meta(self) -> list[ChannelMeta]:
        """List metadata for all registered channels.

        Returns:
            List of ChannelMeta dicts
        """
        return [channel.meta for channel in self.list()]

    def list_ids(self) -> list[str]:
        """List all registered channel IDs.

        Returns:
            List of channel IDs
        """
        return [channel.meta["id"] for channel in self.list()]

    def has(self, channel_id: str) -> bool:
        """Check if a channel is registered.

        Args:
            channel_id: Channel ID or alias

        Returns:
            True if channel exists
        """
        return self.get(channel_id) is not None

    # --- Plugin Discovery ---

    def discover_plugins(
        self,
        plugin_dir: str | Path,
        *,
        pattern: str = "*.py",
    ) -> list[str]:
        """Discover and load channel plugins from a directory.

        Searches for Python modules that define a `register_channel` function
        or export a `ChannelPlugin` subclass.

        Args:
            plugin_dir: Directory to search for plugins
            pattern: Glob pattern for plugin files

        Returns:
            List of loaded plugin IDs
        """
        plugin_path = Path(plugin_dir)
        if not plugin_path.exists():
            logger.warning(f"Plugin directory not found: {plugin_path}")
            return []

        loaded: list[str] = []

        for module_path in plugin_path.glob(pattern):
            if module_path.name.startswith("_"):
                continue

            module_name = module_path.stem
            if module_name in self._loaded_plugins:
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"zerg.channels.plugins.{module_name}",
                    module_path,
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Check for register_channel function
                    if hasattr(module, "register_channel"):
                        channel = module.register_channel()
                        if isinstance(channel, ChannelPlugin):
                            self.register(channel)
                            loaded.append(channel.meta["id"])

                    self._loaded_plugins.add(module_name)

            except Exception as e:
                logger.error(f"Failed to load plugin {module_name}: {e}")

        return loaded

    def load_plugin_module(self, module_path: str) -> ChannelPlugin | None:
        """Load a channel plugin from a module path.

        Args:
            module_path: Dotted module path (e.g., "mypackage.telegram_channel")

        Returns:
            Loaded ChannelPlugin or None if failed
        """
        try:
            module = importlib.import_module(module_path)

            # Look for register_channel function
            if hasattr(module, "register_channel"):
                channel = module.register_channel()
                if isinstance(channel, ChannelPlugin):
                    self.register(channel)
                    return channel

            # Look for ChannelPlugin subclass
            for name in dir(module):
                obj = getattr(module, name)
                if isinstance(obj, type) and issubclass(obj, ChannelPlugin) and obj is not ChannelPlugin:
                    channel = obj()
                    self.register(channel)
                    return channel

            logger.warning(f"No channel plugin found in module: {module_path}")
            return None

        except Exception as e:
            logger.error(f"Failed to load plugin module {module_path}: {e}")
            return None

    # --- Lifecycle Management ---

    async def start_all(self) -> dict[str, Exception | None]:
        """Start all registered channels.

        Returns:
            Dict mapping channel_id to exception (or None if successful)
        """
        results: dict[str, Exception | None] = {}

        for channel_id, channel in self._channels.items():
            try:
                await channel.start()
                results[channel_id] = None
            except Exception as e:
                logger.error(f"Failed to start channel {channel_id}: {e}")
                results[channel_id] = e

        return results

    async def stop_all(self) -> dict[str, Exception | None]:
        """Stop all registered channels.

        Returns:
            Dict mapping channel_id to exception (or None if successful)
        """
        results: dict[str, Exception | None] = {}

        for channel_id, channel in self._channels.items():
            try:
                await channel.stop()
                results[channel_id] = None
            except Exception as e:
                logger.error(f"Failed to stop channel {channel_id}: {e}")
                results[channel_id] = e

        return results

    def get_status_summary(self) -> dict[str, Any]:
        """Get status summary for all channels.

        Returns:
            Dict with channel status information
        """
        return {
            channel_id: {
                "name": channel.meta.get("name"),
                "status": channel.status.value,
                "capabilities": channel.capabilities,
            }
            for channel_id, channel in self._channels.items()
        }


# --- Global Registry Instance ---

_global_registry: ChannelRegistry | None = None


def get_registry() -> ChannelRegistry:
    """Get the global channel registry instance.

    Creates a new registry if one doesn't exist.

    Returns:
        The global ChannelRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = ChannelRegistry()
    return _global_registry


def register_channel(channel: ChannelPlugin, *, replace: bool = False) -> None:
    """Register a channel with the global registry.

    Convenience function for global registry access.

    Args:
        channel: Channel plugin to register
        replace: If True, replace existing channel
    """
    get_registry().register(channel, replace=replace)


def get_channel(channel_id: str) -> ChannelPlugin | None:
    """Get a channel from the global registry.

    Convenience function for global registry access.

    Args:
        channel_id: Channel ID or alias

    Returns:
        ChannelPlugin if found, None otherwise
    """
    return get_registry().get(channel_id)


def list_channels() -> list[ChannelPlugin]:
    """List all channels in the global registry.

    Convenience function for global registry access.

    Returns:
        List of registered channels
    """
    return get_registry().list()
