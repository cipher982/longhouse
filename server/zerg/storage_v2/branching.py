"""The transcript's head-branch rule, shared by every reader of render records.

A record is off the head branch when its adapter marked it ``abandoned``, or,
for providers whose adapters ship sibling user records instead of marking the
branch, when a later user record has the same parent. The transcript reader
and the session turn index both call this module so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable

OrderKey = tuple[int, str, str, str, str, int, int]

# Providers whose adapters leave a rewind as two user records with the same
# parent link instead of marking the old one abandoned. Pi and OMP also ship
# parent links, but their trees add branch-summary records at branch points,
# so this sibling rule is not proven for them yet.
_PARENT_LINK_BRANCH_PROVIDERS = frozenset({"claude"})


def infers_branches_from_parent_links(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in _PARENT_LINK_BRANCH_PROVIDERS


def superseded_user_event_ids(user_records: Iterable[tuple[OrderKey, str, str | None]]) -> set[str]:
    """Event ids of user records replaced by a later sibling.

    Each item is ``(order_key, event_id, parent_uuid)`` for one user-role
    record. Siblings share a parent; all but the last (by source position,
    then subordinal, then order key) are superseded.
    """

    siblings: dict[str, list[tuple[OrderKey, str]]] = {}
    for key, event_id, parent_uuid in user_records:
        if parent_uuid:
            siblings.setdefault(parent_uuid, []).append((key, event_id))
    superseded: set[str] = set()
    for group in siblings.values():
        group.sort(key=lambda item: (item[0][5], item[0][6], item[0]))
        superseded.update(event_id for _, event_id in group[:-1])
    return superseded
