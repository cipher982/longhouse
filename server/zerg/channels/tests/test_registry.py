"""Tests for the ChannelRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from zerg.channels.plugins.mock import MockChannel
from zerg.channels.registry import ChannelRegistry
from zerg.channels.registry import ChannelRegistryError
from zerg.channels.registry import get_channel
from zerg.channels.registry import get_registry
from zerg.channels.registry import list_channels
from zerg.channels.registry import register_channel
from zerg.channels.types import ChannelStatus


class TestChannelRegistry:
    """Tests for ChannelRegistry class."""

    def test_register_channel(self):
        """Test registering a channel."""
        registry = ChannelRegistry()
        channel = MockChannel()

        registry.register(channel)

        assert registry.has("mock")
        assert registry.get("mock") is channel

    def test_register_duplicate_fails(self):
        """Test registering duplicate channel fails."""
        registry = ChannelRegistry()
        channel1 = MockChannel()
        channel2 = MockChannel()

        registry.register(channel1)

        with pytest.raises(ChannelRegistryError, match="already registered"):
            registry.register(channel2)

    def test_register_duplicate_with_replace(self):
        """Test registering duplicate with replace=True succeeds."""
        registry = ChannelRegistry()
        channel1 = MockChannel()
        channel2 = MockChannel()

        registry.register(channel1)
        registry.register(channel2, replace=True)

        assert registry.get("mock") is channel2

    def test_register_without_id_fails(self):
        """Test registering channel without ID fails."""
        registry = ChannelRegistry()
        channel = MagicMock()
        channel.meta = {}  # No ID

        with pytest.raises(ChannelRegistryError, match="must have an 'id'"):
            registry.register(channel)

    def test_unregister_channel(self):
        """Test unregistering a channel."""
        registry = ChannelRegistry()
        channel = MockChannel()

        registry.register(channel)
        assert registry.has("mock")

        result = registry.unregister("mock")
        assert result is True
        assert not registry.has("mock")

    def test_unregister_nonexistent(self):
        """Test unregistering nonexistent channel."""
        registry = ChannelRegistry()
        result = registry.unregister("nonexistent")
        assert result is False

    def test_get_by_id(self):
        """Test getting channel by ID."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        result = registry.get("mock")
        assert result is channel

    def test_get_by_alias(self):
        """Test getting channel by alias."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        # MockChannel has aliases: ["test", "fake"]
        result = registry.get("test")
        assert result is channel

        result = registry.get("fake")
        assert result is channel

    def test_get_case_insensitive(self):
        """Test that get is case-insensitive."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        assert registry.get("MOCK") is channel
        assert registry.get("Mock") is channel
        assert registry.get("TEST") is channel

    def test_get_nonexistent(self):
        """Test getting nonexistent channel."""
        registry = ChannelRegistry()
        result = registry.get("nonexistent")
        assert result is None

    def test_list_channels(self):
        """Test listing all channels."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        channels = registry.list()
        assert len(channels) == 1
        assert channels[0] is channel

    def test_list_channels_sorted_by_order(self):
        """Test that channels are sorted by order."""
        registry = ChannelRegistry()

        # Create mock channels with different orders
        channel1 = MagicMock()
        channel1.meta = {"id": "ch1", "name": "Channel 1", "order": 10}
        channel1.capabilities = {}

        channel2 = MagicMock()
        channel2.meta = {"id": "ch2", "name": "Channel 2", "order": 5}
        channel2.capabilities = {}

        channel3 = MagicMock()
        channel3.meta = {"id": "ch3", "name": "Channel 3"}  # No order (999)
        channel3.capabilities = {}

        registry.register(channel1)
        registry.register(channel2)
        registry.register(channel3)

        channels = registry.list()
        # Order: ch2 (5), ch1 (10), ch3 (999)
        assert channels[0].meta["id"] == "ch2"
        assert channels[1].meta["id"] == "ch1"
        assert channels[2].meta["id"] == "ch3"

    def test_list_meta(self):
        """Test listing channel metadata."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        metas = registry.list_meta()
        assert len(metas) == 1
        assert metas[0]["id"] == "mock"
        assert metas[0]["name"] == "Mock Channel"

    def test_list_ids(self):
        """Test listing channel IDs."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        ids = registry.list_ids()
        assert ids == ["mock"]

    def test_has(self):
        """Test has method."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        assert registry.has("mock") is True
        assert registry.has("test") is True  # Alias
        assert registry.has("nonexistent") is False


class TestChannelRegistryLifecycle:
    """Tests for registry lifecycle management."""

    @pytest.mark.asyncio
    async def test_start_all(self):
        """Test starting all channels."""
        registry = ChannelRegistry()

        channel = MockChannel()
        await channel.configure({})
        registry.register(channel)

        results = await registry.start_all()

        assert results["mock"] is None  # No error
        assert channel.status == ChannelStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_stop_all(self):
        """Test stopping all channels."""
        registry = ChannelRegistry()

        channel = MockChannel()
        await channel.configure({})
        registry.register(channel)
        await channel.start()

        results = await registry.stop_all()

        assert results["mock"] is None  # No error
        assert channel.status == ChannelStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_start_all_handles_errors(self):
        """Test that start_all handles individual channel errors."""
        registry = ChannelRegistry()

        # Create a channel that fails to start
        failing_channel = MagicMock()
        failing_channel.meta = {"id": "failing"}
        failing_channel.start = AsyncMock(side_effect=ConnectionError("Failed"))
        registry.register(failing_channel)

        # Create a channel that succeeds
        good_channel = MockChannel()
        await good_channel.configure({})
        registry.register(good_channel)

        results = await registry.start_all()

        assert isinstance(results["failing"], Exception)
        assert results["mock"] is None  # Success

    def test_get_status_summary(self):
        """Test getting status summary."""
        registry = ChannelRegistry()
        channel = MockChannel()
        registry.register(channel)

        summary = registry.get_status_summary()

        assert "mock" in summary
        assert summary["mock"]["name"] == "Mock Channel"
        assert summary["mock"]["status"] == "disconnected"


class TestGlobalRegistry:
    """Tests for global registry functions."""

    def test_get_registry_singleton(self):
        """Test that get_registry returns same instance."""
        reg1 = get_registry()
        reg2 = get_registry()
        assert reg1 is reg2

    def test_register_channel_global(self):
        """Test global register_channel function."""
        # Clear any existing channels first
        registry = get_registry()
        registry._channels.clear()
        registry._aliases.clear()

        channel = MockChannel()
        register_channel(channel)

        assert get_channel("mock") is channel

    def test_get_channel_global(self):
        """Test global get_channel function."""
        registry = get_registry()
        registry._channels.clear()
        registry._aliases.clear()

        channel = MockChannel()
        registry.register(channel)

        result = get_channel("mock")
        assert result is channel

    def test_list_channels_global(self):
        """Test global list_channels function."""
        registry = get_registry()
        registry._channels.clear()
        registry._aliases.clear()

        channel = MockChannel()
        registry.register(channel)

        channels = list_channels()
        assert len(channels) == 1
        assert channels[0] is channel
