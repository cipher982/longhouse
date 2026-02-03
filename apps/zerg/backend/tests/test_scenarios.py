"""Tests for scenario seeding lifecycle and idempotency.

Covers:
- Idempotent seeding (multiple seeds don't duplicate)
- Cleanup deletes data and registry
- Cleanup enables reseed (regression test for registry clearing)
- SeedRegistry prevents duplicates
"""

import pytest

from zerg.models.models import CommisJob, Run, SeedRegistry, Thread, ThreadMessage
from zerg.models.run_event import RunEvent
from zerg.scenarios.seed import (
    cleanup_scenario,
    check_seed_registry,
    register_seed,
    seed_scenario,
)


class TestScenarioSeeding:
    """Tests for scenario seeding operations."""

    def test_seed_scenario_idempotent(self, db_session, test_user):
        """Multiple seeds don't duplicate - uses SeedRegistry."""
        scenario_name = "swarm-mvp"

        # First seed
        result1 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result1["runs"] == 5
        assert result1["skipped"] == 0
        assert result1["scenario"] == scenario_name

        # Get counts after first seed
        runs_count = db_session.query(Run).count()
        threads_count = db_session.query(Thread).count()
        registry_count = db_session.query(SeedRegistry).count()

        assert runs_count == 5
        assert threads_count == 5
        assert registry_count == 5

        # Second seed - should skip all runs
        result2 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result2["runs"] == 0
        assert result2["skipped"] == 5  # All runs already seeded

        # Counts should be unchanged
        assert db_session.query(Run).count() == runs_count
        assert db_session.query(Thread).count() == threads_count
        assert db_session.query(SeedRegistry).count() == registry_count

    def test_cleanup_scenario_deletes_data(self, db_session, test_user):
        """Cleanup removes threads, runs, messages, registry."""
        scenario_name = "swarm-mvp"

        # Seed scenario
        result = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result["runs"] == 5

        # Verify data exists
        threads = db_session.query(Thread).all()
        runs = db_session.query(Run).all()
        registry_entries = db_session.query(SeedRegistry).all()

        assert len(threads) == 5
        assert len(runs) == 5
        assert len(registry_entries) == 5

        # Cleanup
        cleanup_result = cleanup_scenario(db_session, scenario_name)

        # Verify deletion counts
        assert cleanup_result["runs"] == 5
        assert cleanup_result["threads"] == 5
        assert cleanup_result["registry_entries"] == 5

        # Verify all data deleted
        assert db_session.query(Thread).count() == 0
        assert db_session.query(Run).count() == 0
        assert db_session.query(SeedRegistry).count() == 0

    def test_cleanup_enables_reseed(self, db_session, test_user):
        """After cleanup, scenario can be reseeded with fresh registry.

        This is the key regression test - cleanup must clear SeedRegistry
        to allow reseeding, otherwise second seed will skip all runs.
        """
        scenario_name = "swarm-mvp"

        # First seed
        result1 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result1["runs"] == 5
        assert result1["skipped"] == 0

        # Verify registry populated
        registry_count = (
            db_session.query(SeedRegistry)
            .filter(SeedRegistry.seed_key.like(f"{scenario_name}:%"))
            .count()
        )
        assert registry_count == 5

        # Cleanup
        cleanup_result = cleanup_scenario(db_session, scenario_name)
        assert cleanup_result["runs"] == 5
        assert cleanup_result["registry_entries"] == 5  # KEY: registry cleared

        # Verify registry cleared
        registry_count = (
            db_session.query(SeedRegistry)
            .filter(SeedRegistry.seed_key.like(f"{scenario_name}:%"))
            .count()
        )
        assert registry_count == 0

        # Reseed after cleanup - should NOT skip
        result2 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result2["runs"] == 5
        assert result2["skipped"] == 0  # This is the regression test!

        # Verify new registry entries created
        registry_count = (
            db_session.query(SeedRegistry)
            .filter(SeedRegistry.seed_key.like(f"{scenario_name}:%"))
            .count()
        )
        assert registry_count == 5

        # Reseed again - should skip (idempotency restored)
        result3 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result3["runs"] == 0
        assert result3["skipped"] == 5

    def test_clean_flag_breaks_idempotency(self, db_session, test_user):
        """clean=True deletes data before seeding (not idempotent)."""
        scenario_name = "swarm-mvp"

        # First seed
        result1 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result1["runs"] == 5

        # Second seed with clean=True - should reseed
        result2 = seed_scenario(
            db_session,
            scenario_name,
            owner_id=test_user.id,
            target="test",
            clean=True,
        )
        assert result2["runs"] == 5
        assert result2["skipped"] == 0  # clean=True bypasses registry check

        # Total runs should still be 5 (old deleted, new created)
        assert db_session.query(Run).count() == 5

    def test_seed_scenario_creates_all_entities(self, db_session, test_user):
        """Seeding creates threads, runs, messages, events, commis jobs."""
        scenario_name = "swarm-mvp"

        result = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )

        # Verify entity creation
        assert result["runs"] == 5
        assert result["messages"] > 0  # Some runs have messages
        assert result["events"] > 0  # Some runs have events

        # Check database
        threads = db_session.query(Thread).all()
        runs = db_session.query(Run).all()
        messages = db_session.query(ThreadMessage).all()
        events = db_session.query(RunEvent).all()

        assert len(threads) == 5
        assert len(runs) == 5
        assert len(messages) == result["messages"]
        assert len(events) == result["events"]

        # Verify thread titles have scenario prefix
        for thread in threads:
            assert thread.title.startswith(f"[scenario:{scenario_name}]")

    def test_different_targets_are_independent(self, db_session, test_user):
        """Same scenario can be seeded to different targets independently."""
        scenario_name = "swarm-mvp"

        # Seed to target "test"
        result1 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="test"
        )
        assert result1["runs"] == 5
        assert result1["skipped"] == 0

        # Seed to target "demo" - should not skip (different target)
        result2 = seed_scenario(
            db_session, scenario_name, owner_id=test_user.id, target="demo"
        )
        assert result2["runs"] == 5
        assert result2["skipped"] == 0

        # Total runs should be 10 (5 per target)
        assert db_session.query(Run).count() == 10

        # Registry should have 10 entries (5 per target)
        assert db_session.query(SeedRegistry).count() == 10


class TestSeedRegistry:
    """Tests for SeedRegistry check and register functions."""

    def test_check_seed_registry_prevents_duplicate(self, db_session):
        """SeedRegistry check prevents duplicate seeding."""
        seed_key = "test-scenario:run:001"
        target = "test"

        # First check - should be None
        existing = check_seed_registry(db_session, seed_key, target)
        assert existing is None

        # Register the seed
        entry = register_seed(
            db_session,
            seed_key=seed_key,
            target=target,
            namespace="test",
            entity_type="run",
            entity_id="123",
        )
        db_session.commit()

        # Second check - should return the entry
        existing = check_seed_registry(db_session, seed_key, target)
        assert existing is not None
        assert existing.seed_key == seed_key
        assert existing.target == target
        assert existing.entity_id == "123"

    def test_register_seed_updates_timestamp(self, db_session):
        """Duplicate seed_key updates timestamp, not count."""
        seed_key = "test-scenario:run:001"
        target = "test"

        # First registration
        entry1 = register_seed(
            db_session,
            seed_key=seed_key,
            target=target,
            namespace="test",
            entity_type="run",
            entity_id="123",
        )
        db_session.commit()
        timestamp1 = entry1.updated_at

        # Second registration with same key
        entry2 = register_seed(
            db_session,
            seed_key=seed_key,
            target=target,
            namespace="test",
            entity_type="run",
            entity_id="123",
        )
        db_session.commit()

        # Should be same entry (no new row)
        assert entry1.id == entry2.id

        # Timestamp should be updated
        assert entry2.updated_at >= timestamp1

        # Only one entry in database
        count = (
            db_session.query(SeedRegistry)
            .filter(
                SeedRegistry.seed_key == seed_key,
                SeedRegistry.target == target,
            )
            .count()
        )
        assert count == 1

    def test_different_targets_create_separate_entries(self, db_session):
        """Same seed_key with different targets creates separate entries."""
        seed_key = "test-scenario:run:001"

        # Register for target "test"
        entry1 = register_seed(
            db_session,
            seed_key=seed_key,
            target="test",
            namespace="test",
            entity_type="run",
            entity_id="123",
        )

        # Register for target "demo"
        entry2 = register_seed(
            db_session,
            seed_key=seed_key,
            target="demo",
            namespace="demo",
            entity_type="run",
            entity_id="456",
        )
        db_session.commit()

        # Should have two separate entries
        assert entry1.id != entry2.id

        # Verify both exist
        test_entry = check_seed_registry(db_session, seed_key, "test")
        demo_entry = check_seed_registry(db_session, seed_key, "demo")

        assert test_entry is not None
        assert demo_entry is not None
        assert test_entry.entity_id == "123"
        assert demo_entry.entity_id == "456"
