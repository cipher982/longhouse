"""Closing a Shadow session on absence requires the engine to be able to see it.

`MISSING_UNBOUND_UNMANAGED_PROVIDERS` gates
`_runtime_events_for_missing_unbound_unmanaged_sessions`, which emits
`process_gone` for sessions missing from a snapshot the engine marked complete.
That makes it dangerous in both directions:

- Too narrow and unmanaged sessions of a covered provider never close. OpenCode
  sat in that state for 87 days: `engine/src/unmanaged_bindings.rs` emits
  opencode bindings and this set omitted it.
- Too wide and every live session of an uncovered provider is closed after the
  grace period, because absence is read as evidence the process is gone. Cursor
  is in that state today -- the engine has no cursor branch and cursor
  transcripts live in SQLite rather than per-session files.

So this list cannot simply derive from the provider contract. What it can do is
refuse to let a provider fall through unclassified.
"""

from __future__ import annotations

from zerg.routers.heartbeat import MANAGED_SESSION_LEASE_PROVIDERS
from zerg.routers.heartbeat import MISSING_UNBOUND_UNMANAGED_PROVIDERS
from zerg.routers.heartbeat import UNMANAGED_PROCESS_SNAPSHOT_UNCOVERED_PROVIDERS
from zerg.services.managed_provider_contracts import factory_provider_names


def test_every_provider_is_explicitly_classified_for_snapshot_coverage() -> None:
    """A new provider must be a decision, not a default.

    Silence here is what produced both failure modes. Adding a provider to the
    schema and neither list should break the build and make someone answer
    whether the engine can observe its unmanaged processes.
    """

    classified = set(MISSING_UNBOUND_UNMANAGED_PROVIDERS) | set(UNMANAGED_PROCESS_SNAPSHOT_UNCOVERED_PROVIDERS)
    for provider in factory_provider_names(include_maintenance=True):
        assert provider in classified, (
            f"{provider} is neither covered by the engine's unmanaged process snapshot nor "
            "recorded as uncovered. Decide: if engine/src/unmanaged_bindings.rs can resolve its "
            "processes, add it to MISSING_UNBOUND_UNMANAGED_PROVIDERS; if it cannot, add it to "
            "UNMANAGED_PROCESS_SNAPSHOT_UNCOVERED_PROVIDERS so absence is never read as death."
        )


def test_coverage_classes_are_disjoint() -> None:
    overlap = set(MISSING_UNBOUND_UNMANAGED_PROVIDERS) & set(UNMANAGED_PROCESS_SNAPSHOT_UNCOVERED_PROVIDERS)
    assert not overlap, f"providers cannot be both covered and uncovered: {sorted(overlap)}"


def test_cursor_stays_out_of_the_close_on_absence_path() -> None:
    """Pin the dangerous direction by name.

    If cursor is ever added here, `engine/src/unmanaged_bindings.rs` must have
    learned to emit cursor bindings first -- otherwise every live unmanaged
    Cursor session gets a `process_gone` terminal event after 90 seconds.
    """

    assert "cursor" not in MISSING_UNBOUND_UNMANAGED_PROVIDERS
    assert "cursor" in UNMANAGED_PROCESS_SNAPSHOT_UNCOVERED_PROVIDERS


def test_opencode_is_covered_because_the_engine_emits_its_bindings() -> None:
    assert "opencode" in MISSING_UNBOUND_UNMANAGED_PROVIDERS


def test_managed_lease_metric_labels_cover_every_provider() -> None:
    """Lease metrics bucketed Cursor as "other" for 31 days."""

    for provider in factory_provider_names(include_maintenance=True):
        assert provider in MANAGED_SESSION_LEASE_PROVIDERS
