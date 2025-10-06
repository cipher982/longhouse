"""Regression test – unprocessed user messages must be picked up by the helper.

Prior to bug-fix #6929 the boolean filter inside
`crud.get_unprocessed_messages` used the Python `not` operator on a
SQLAlchemy column which yielded a constant ``False`` expression and caused
the query to always return an empty list.  As a result the runner never
generated an assistant reply.

This test ensures the helper returns the expected rows so that any future
regression will be caught immediately.
"""

from __future__ import annotations


def test_get_unprocessed_messages_returns_pending(db_session, sample_agent):
    """Create a thread → insert an *unprocessed* message → expect it back."""

    from zerg.crud import crud
    from zerg.services.thread_service import ThreadService

    # 1. Create fresh thread (also inserts system message)
    thread = ThreadService.create_thread_with_system_message(db_session, sample_agent, title="bug-guard")

    # 2. Insert a *user* message marked as *unprocessed*
    inserted = crud.create_thread_message(
        db_session,
        thread_id=thread.id,
        role="user",
        content="hi",
        processed=False,
    )

    # 3. Helper must return exactly this row
    rows = crud.get_unprocessed_messages(db_session, thread_id=thread.id)

    assert rows == [inserted]
