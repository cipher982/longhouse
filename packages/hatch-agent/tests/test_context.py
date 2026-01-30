"""Tests for context detection."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from hatch.context import (
    ExecutionContext,
    _check_home_writable,
    _detect_container,
    clear_context_cache,
    detect_context,
)


class TestExecutionContext:
    """Tests for ExecutionContext dataclass."""

    def test_laptop_effective_home(self):
        """Laptop context uses actual HOME."""
        ctx = ExecutionContext(in_container=False, home_writable=True)
        with mock.patch.dict(os.environ, {"HOME": "/Users/test"}):
            assert ctx.effective_home == "/Users/test"

    def test_container_writable_home(self):
        """Container with writable home uses actual HOME."""
        ctx = ExecutionContext(in_container=True, home_writable=True)
        with mock.patch.dict(os.environ, {"HOME": "/home/app"}):
            assert ctx.effective_home == "/home/app"

    def test_container_readonly_home(self):
        """Container with read-only home uses /tmp."""
        ctx = ExecutionContext(in_container=True, home_writable=False)
        assert ctx.effective_home == "/tmp"

    def test_no_home_env(self):
        """Missing HOME env var defaults to /tmp."""
        ctx = ExecutionContext(in_container=False, home_writable=True)
        env_without_home = {k: v for k, v in os.environ.items() if k != "HOME"}
        with mock.patch.dict(os.environ, env_without_home, clear=True):
            assert ctx.effective_home == "/tmp"

    def test_frozen_dataclass(self):
        """ExecutionContext is immutable."""
        ctx = ExecutionContext(in_container=False, home_writable=True)
        with pytest.raises(AttributeError):
            ctx.in_container = True  # type: ignore


class TestContainerDetection:
    """Tests for container detection logic."""

    def test_no_container_indicators(self):
        """No container when no indicators present."""
        with mock.patch("os.path.exists", return_value=False):
            with mock.patch("builtins.open", side_effect=FileNotFoundError):
                assert _detect_container() is False

    def test_dockerenv_present(self):
        """Docker detected via /.dockerenv."""

        def mock_exists(path):
            return path == "/.dockerenv"

        with mock.patch("os.path.exists", side_effect=mock_exists):
            assert _detect_container() is True

    def test_podman_containerenv(self):
        """Podman detected via /run/.containerenv."""

        def mock_exists(path):
            return path == "/run/.containerenv"

        with mock.patch("os.path.exists", side_effect=mock_exists):
            assert _detect_container() is True

    def test_cgroup_docker(self):
        """Container detected via cgroup containing 'docker'."""
        with mock.patch("os.path.exists", return_value=False):
            mock_file = mock.mock_open(read_data="12:devices:/docker/abc123\n")
            with mock.patch("builtins.open", mock_file):
                assert _detect_container() is True

    def test_cgroup_containerd(self):
        """Container detected via cgroup containing 'containerd'."""
        with mock.patch("os.path.exists", return_value=False):
            mock_file = mock.mock_open(
                read_data="0::/system.slice/containerd.service\n"
            )
            with mock.patch("builtins.open", mock_file):
                assert _detect_container() is True

    def test_cgroup_kubepods(self):
        """Container detected via cgroup containing 'kubepods'."""
        with mock.patch("os.path.exists", return_value=False):
            mock_file = mock.mock_open(
                read_data="0::/kubepods/besteffort/pod123\n"
            )
            with mock.patch("builtins.open", mock_file):
                assert _detect_container() is True

    def test_cgroup_permission_denied(self):
        """Permission denied reading cgroup is handled."""
        with mock.patch("os.path.exists", return_value=False):
            with mock.patch("builtins.open", side_effect=PermissionError):
                assert _detect_container() is False


class TestHomeWritableCheck:
    """Tests for home writable check."""

    def test_writable_home(self, tmp_path: Path):
        """Writable home directory detected."""
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            assert _check_home_writable() is True

    def test_readonly_home(self):
        """Read-only home directory detected."""
        with mock.patch.dict(os.environ, {"HOME": "/nonexistent/readonly"}):
            # Path.write_text will raise OSError
            assert _check_home_writable() is False

    def test_home_not_set(self):
        """Missing HOME env var uses /root (likely not writable)."""
        env_without_home = {k: v for k, v in os.environ.items() if k != "HOME"}
        with mock.patch.dict(os.environ, env_without_home, clear=True):
            # /root is typically not writable by non-root
            result = _check_home_writable()
            # Result depends on actual permissions, just verify it runs
            assert isinstance(result, bool)


class TestDetectContext:
    """Tests for the main detect_context function."""

    def test_caches_result(self):
        """detect_context caches its result."""
        clear_context_cache()
        ctx1 = detect_context()
        ctx2 = detect_context()
        assert ctx1 is ctx2

    def test_cache_clear_works(self):
        """clear_context_cache actually clears the cache."""
        clear_context_cache()

        # Mock different returns for each call
        call_count = [0]

        def mock_container():
            call_count[0] += 1
            return call_count[0] == 1  # True first time, False second

        with mock.patch(
            "hatch.context._detect_container", side_effect=mock_container
        ):
            with mock.patch(
                "hatch.context._check_home_writable", return_value=True
            ):
                ctx1 = detect_context()
                assert ctx1.in_container is True

                clear_context_cache()
                ctx2 = detect_context()
                assert ctx2.in_container is False

    def test_returns_execution_context(self):
        """detect_context returns ExecutionContext instance."""
        clear_context_cache()
        ctx = detect_context()
        assert isinstance(ctx, ExecutionContext)


class TestContextIntegration:
    """Integration tests for context detection on real system."""

    def test_real_detection_runs(self):
        """Real detection runs without error on current system."""
        clear_context_cache()
        ctx = detect_context()

        # On macOS laptop, should not be in container
        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            assert ctx.in_container is True
        else:
            # Might still be container via cgroup, so just verify it's a bool
            assert isinstance(ctx.in_container, bool)

        # Home should typically be writable
        assert isinstance(ctx.home_writable, bool)
