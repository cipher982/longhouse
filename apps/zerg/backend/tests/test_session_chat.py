"""Tests for session chat functionality (Forum drop-in).

This module tests the session lock manager, workspace resolver, and related
functionality for the Forum drop-in chat feature.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestSessionLockManagerAcquireRelease:
    """Tests for basic lock acquisition and release."""

    @pytest.mark.asyncio
    async def test_session_lock_manager_acquire_release(self):
        """Basic lock acquisition and release works correctly."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "test-session-123"
        holder = "test-holder"

        # Acquire lock
        lock = await manager.acquire(session_id, holder=holder)
        assert lock is not None
        assert lock.session_id == session_id
        assert lock.holder == holder
        assert not lock.is_expired

        # Verify locked
        assert await manager.is_locked(session_id)

        # Release lock
        released = await manager.release(session_id, holder=holder)
        assert released is True

        # Verify unlocked
        assert not await manager.is_locked(session_id)

    @pytest.mark.asyncio
    async def test_session_lock_manager_release_without_holder(self):
        """Lock can be released without specifying holder (force release)."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "test-session-456"

        # Acquire lock
        lock = await manager.acquire(session_id, holder="holder-1")
        assert lock is not None

        # Release without specifying holder
        released = await manager.release(session_id)
        assert released is True

        # Verify unlocked
        assert not await manager.is_locked(session_id)


class TestSessionLockManagerBlocksSecondAcquire:
    """Tests for lock contention."""

    @pytest.mark.asyncio
    async def test_session_lock_manager_blocks_second_acquire(self):
        """Second acquire attempt returns None when session is already locked."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "contended-session"

        # First holder acquires lock
        lock1 = await manager.acquire(session_id, holder="holder-1")
        assert lock1 is not None

        # Second holder tries to acquire - should fail
        lock2 = await manager.acquire(session_id, holder="holder-2")
        assert lock2 is None

        # Verify lock info shows first holder
        lock_info = await manager.get_lock_info(session_id)
        assert lock_info is not None
        assert lock_info.holder == "holder-1"

    @pytest.mark.asyncio
    async def test_session_lock_manager_same_holder_cannot_reacquire(self):
        """Same holder cannot acquire lock twice."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "same-holder-session"
        holder = "holder-1"

        # First acquire
        lock1 = await manager.acquire(session_id, holder=holder)
        assert lock1 is not None

        # Same holder tries again - should fail (lock is held)
        lock2 = await manager.acquire(session_id, holder=holder)
        assert lock2 is None


class TestSessionLockManagerTTLExpiration:
    """Tests for TTL-based lock expiration."""

    @pytest.mark.asyncio
    async def test_session_lock_manager_ttl_expiration(self):
        """Lock expires after TTL and can be reacquired."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "expiring-session"

        # Acquire lock with very short TTL
        lock = await manager.acquire(session_id, holder="holder-1", ttl_seconds=1)
        assert lock is not None
        assert not lock.is_expired

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Lock should now be expired
        assert lock.is_expired
        assert lock.time_remaining == 0

        # Should be able to reacquire by different holder
        lock2 = await manager.acquire(session_id, holder="holder-2")
        assert lock2 is not None
        assert lock2.holder == "holder-2"

    @pytest.mark.asyncio
    async def test_session_lock_manager_time_remaining(self):
        """time_remaining property decreases over time."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "timing-session"

        lock = await manager.acquire(session_id, holder="holder-1", ttl_seconds=10)
        assert lock is not None

        initial_remaining = lock.time_remaining
        assert 9 < initial_remaining <= 10

        await asyncio.sleep(0.5)

        later_remaining = lock.time_remaining
        assert later_remaining < initial_remaining


class TestSessionLockManagerCleanupExpired:
    """Tests for cleanup_expired functionality."""

    @pytest.mark.asyncio
    async def test_session_lock_manager_cleanup_expired(self):
        """cleanup_expired removes all expired locks."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()

        # Create multiple locks with short TTL
        for i in range(5):
            await manager.acquire(f"session-{i}", holder=f"holder-{i}", ttl_seconds=1)

        # All should be locked
        assert await manager.is_locked("session-0")
        assert await manager.is_locked("session-4")

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Cleanup
        cleaned = await manager.cleanup_expired()
        assert cleaned == 5

        # All should be unlocked now
        for i in range(5):
            assert not await manager.is_locked(f"session-{i}")

    @pytest.mark.asyncio
    async def test_session_lock_manager_cleanup_mixed_expiration(self):
        """cleanup_expired only removes expired locks, keeps valid ones."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()

        # Create mix of short and long TTL locks
        await manager.acquire("short-1", holder="h1", ttl_seconds=1)
        await manager.acquire("short-2", holder="h2", ttl_seconds=1)
        await manager.acquire("long-1", holder="h3", ttl_seconds=300)
        await manager.acquire("long-2", holder="h4", ttl_seconds=300)

        # Wait for short ones to expire
        await asyncio.sleep(1.1)

        # Cleanup
        cleaned = await manager.cleanup_expired()
        assert cleaned == 2  # Only short TTL locks

        # Long TTL locks should still be held
        assert await manager.is_locked("long-1")
        assert await manager.is_locked("long-2")
        assert not await manager.is_locked("short-1")
        assert not await manager.is_locked("short-2")

    @pytest.mark.asyncio
    async def test_session_lock_manager_opportunistic_cleanup_on_acquire(self):
        """acquire() opportunistically cleans up expired locks."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()

        # Create expired locks
        for i in range(3):
            await manager.acquire(f"old-session-{i}", holder=f"h{i}", ttl_seconds=1)

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Acquire a new lock - should trigger cleanup
        lock = await manager.acquire("new-session", holder="new-holder")
        assert lock is not None

        # Old locks should be cleaned up (internal state check)
        assert len(manager._locks) == 1  # Only the new lock

    @pytest.mark.asyncio
    async def test_session_lock_manager_opportunistic_cleanup_on_get_lock_info(self):
        """get_lock_info() opportunistically cleans up expired locks."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()

        # Create expired locks
        for i in range(3):
            await manager.acquire(f"old-session-{i}", holder=f"h{i}", ttl_seconds=1)

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Get lock info for non-existent session - should trigger cleanup
        info = await manager.get_lock_info("non-existent")
        assert info is None

        # Old locks should be cleaned up
        assert len(manager._locks) == 0


class TestWorkspaceResolverLocalPath:
    """Tests for WorkspaceResolver using local paths."""

    @pytest.mark.asyncio
    async def test_workspace_resolver_local_path(self, tmp_path):
        """Uses local path when it exists."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        # Create a local workspace
        local_workspace = tmp_path / "my_project"
        local_workspace.mkdir()
        (local_workspace / "README.md").write_text("# My Project")

        # Resolve should use local path
        result = await resolver.resolve(
            original_cwd=str(local_workspace),
            git_repo="https://github.com/example/repo.git",
            git_branch="main",
        )

        assert result.path == local_workspace
        assert result.is_temp is False
        assert result.error is None

    @pytest.mark.asyncio
    async def test_workspace_resolver_prefers_local_over_git(self, tmp_path):
        """Local path takes precedence over git clone."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        # Create a local workspace
        local_workspace = tmp_path / "existing_project"
        local_workspace.mkdir()

        # Resolve with both local and git options
        result = await resolver.resolve(
            original_cwd=str(local_workspace),
            git_repo="https://github.com/example/repo.git",
            git_branch="main",
        )

        # Should use local path, not clone
        assert result.path == local_workspace
        assert result.is_temp is False


class TestWorkspaceResolverMissingPathNoRepo:
    """Tests for WorkspaceResolver error cases."""

    @pytest.mark.asyncio
    async def test_workspace_resolver_missing_path_no_repo(self, tmp_path):
        """Returns error when no workspace available."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        # Non-existent path and no git repo
        result = await resolver.resolve(
            original_cwd="/non/existent/path",
            git_repo=None,
            git_branch=None,
        )

        assert result.error is not None
        assert "No workspace available" in result.error

    @pytest.mark.asyncio
    async def test_workspace_resolver_none_cwd_no_repo(self, tmp_path):
        """Returns error when cwd is None and no git repo."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        result = await resolver.resolve(
            original_cwd=None,
            git_repo=None,
            git_branch=None,
        )

        assert result.error is not None
        assert "No workspace available" in result.error

    @pytest.mark.asyncio
    async def test_workspace_resolver_file_not_directory(self, tmp_path):
        """Returns error when path exists but is a file, not directory."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        # Create a file (not directory)
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("I'm a file")

        result = await resolver.resolve(
            original_cwd=str(file_path),
            git_repo=None,
            git_branch=None,
        )

        assert result.error is not None
        assert "No workspace available" in result.error


class TestWorkspaceResolverGitClone:
    """Tests for WorkspaceResolver git clone functionality."""

    @pytest.mark.asyncio
    async def test_workspace_resolver_git_clone_failure(self, tmp_path):
        """Returns error when git clone fails."""
        from zerg.services.session_continuity import WorkspaceResolver

        resolver = WorkspaceResolver(temp_base=tmp_path / "temp_workspaces")

        # Use invalid git repo URL
        result = await resolver.resolve(
            original_cwd="/non/existent/path",
            git_repo="invalid-url-not-a-repo",
            git_branch="main",
            session_id="test-session-123",
        )

        assert result.error is not None
        assert "clone" in result.error.lower() or "failed" in result.error.lower()
        assert result.is_temp is True

    @pytest.mark.asyncio
    async def test_workspace_resolver_cleanup(self, tmp_path):
        """ResolvedWorkspace.cleanup() removes temp directory."""
        from zerg.services.session_continuity import ResolvedWorkspace

        # Create a temp workspace
        temp_workspace = tmp_path / "temp_ws"
        temp_workspace.mkdir()
        (temp_workspace / "file.txt").write_text("test")

        workspace = ResolvedWorkspace(
            path=temp_workspace,
            is_temp=True,
        )

        # Cleanup should remove it
        workspace.cleanup()
        assert not temp_workspace.exists()

    @pytest.mark.asyncio
    async def test_workspace_resolver_cleanup_non_temp(self, tmp_path):
        """ResolvedWorkspace.cleanup() does not remove non-temp directory."""
        from zerg.services.session_continuity import ResolvedWorkspace

        # Create a non-temp workspace
        local_workspace = tmp_path / "local_ws"
        local_workspace.mkdir()
        (local_workspace / "important.txt").write_text("keep me")

        workspace = ResolvedWorkspace(
            path=local_workspace,
            is_temp=False,
        )

        # Cleanup should NOT remove it
        workspace.cleanup()
        assert local_workspace.exists()
        assert (local_workspace / "important.txt").exists()


class TestSessionLockManagerHolderMismatch:
    """Tests for holder validation during release."""

    @pytest.mark.asyncio
    async def test_release_with_wrong_holder_fails(self):
        """Release fails when holder doesn't match."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()
        session_id = "protected-session"

        # Acquire with holder-1
        lock = await manager.acquire(session_id, holder="holder-1")
        assert lock is not None

        # Try to release with holder-2
        released = await manager.release(session_id, holder="holder-2")
        assert released is False

        # Lock should still be held by holder-1
        lock_info = await manager.get_lock_info(session_id)
        assert lock_info is not None
        assert lock_info.holder == "holder-1"

    @pytest.mark.asyncio
    async def test_release_nonexistent_session(self):
        """Release returns False for non-existent session."""
        from zerg.services.session_continuity import SessionLockManager

        manager = SessionLockManager()

        released = await manager.release("non-existent-session", holder="any")
        assert released is False
