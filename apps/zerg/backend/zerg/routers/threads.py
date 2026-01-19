"""
Router for thread-related endpoints.

This module provides API endpoints for creating, retrieving, updating,
and interacting with threads.
"""

import logging
from typing import List
from typing import Optional

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

# New higher-level ThreadService façade
# Auth dependency
from zerg.callbacks.token_stream import set_current_user_id

# DB/CRUD helpers
from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.generated.ws_messages import AssistantIdData
from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import StreamChunkData
from zerg.generated.ws_messages import StreamEndData
from zerg.generated.ws_messages import StreamStartData
from zerg.managers.agent_runner import AgentRunner
from zerg.schemas.schemas import Thread
from zerg.schemas.schemas import ThreadCreate
from zerg.schemas.schemas import ThreadMessageCreate
from zerg.schemas.schemas import ThreadMessageResponse
from zerg.schemas.schemas import ThreadSummary
from zerg.schemas.schemas import ThreadUpdate
from zerg.services.quota import assert_can_start_run
from zerg.services.run_history import execute_thread_run_with_history

# Thread service façade
from zerg.services.thread_service import ThreadService

# WebSocket topic manager
from zerg.websocket.manager import topic_manager

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["threads"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/", response_model=List[ThreadSummary])
@router.get("", response_model=List[ThreadSummary])
def read_threads(
    agent_id: Optional[int] = None,
    thread_type: Optional[str] = None,
    title: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get all threads, optionally filtered by agent_id, thread_type, and/or title.

    If `title` is provided, returns threads matching that title.
    """
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    owner_id = None if is_admin else current_user.id
    threads = crud.get_threads(
        db,
        owner_id=owner_id,
        agent_id=agent_id,
        thread_type=thread_type,
        title=title,
        skip=skip,
        limit=limit,
    )
    if not threads:
        return []
    return threads


@router.post("/", response_model=Thread, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=Thread, status_code=status.HTTP_201_CREATED)
def create_thread(thread: ThreadCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Create a new thread"""
    # Ensure agent exists and fetch row
    agent_row = crud.get_agent(db, agent_id=thread.agent_id)
    if agent_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Authorization: only owner (or admin) can create a thread for an agent
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent_row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

    # Delegate creation to ThreadService so the mandatory system message is
    # inserted atomically.
    created_thread = ThreadService.create_thread_with_system_message(
        db,
        agent_row,
        title=thread.title,
        thread_type=thread.thread_type or "chat",
        active=thread.active,
    )

    # If the request supplied agent_state or memory_strategy we update the
    # thread accordingly (ThreadService currently doesn't take those extras
    # to keep the helper minimal).
    if thread.agent_state or thread.memory_strategy != "buffer":
        _ = crud.update_thread(
            db,
            thread_id=created_thread.id,
            agent_state=thread.agent_state,
            memory_strategy=thread.memory_strategy,
        )

    return created_thread


@router.get("/{thread_id}", response_model=Thread)
def read_thread(thread_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Get a specific thread by ID"""
    db_thread = crud.get_thread(db, thread_id=thread_id)
    if db_thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    # Authorization: only owner or admin can read
    agent = crud.get_agent(db, agent_id=db_thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")
    return db_thread


@router.put("/{thread_id}", response_model=Thread)
def update_thread(thread_id: int, thread: ThreadUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Update a thread"""
    db_thread = crud.get_thread(db, thread_id=thread_id)
    if db_thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    agent = crud.get_agent(db, agent_id=db_thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")

    db_thread = crud.update_thread(
        db,
        thread_id=thread_id,
        title=thread.title,
        active=thread.active,
        agent_state=thread.agent_state,
        memory_strategy=thread.memory_strategy,
        thread_type=thread.thread_type,
    )
    return db_thread


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(thread_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Delete a thread"""
    db_thread = crud.get_thread(db, thread_id=thread_id)
    if db_thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    agent = crud.get_agent(db, agent_id=db_thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")

    if not crud.delete_thread(db, thread_id=thread_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    return None


@router.get("/{thread_id}/messages", response_model=List[ThreadMessageResponse])
def read_thread_messages(
    thread_id: int,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get all messages for a thread.

    IMPORTANT: Messages are returned strictly ordered by database ID (insertion order).
    This provides deterministic ordering regardless of timestamp precision or creation time.
    The client MUST NOT sort these messages client-side; the server ordering is authoritative.

    See crud.get_thread_messages() for implementation details on the .order_by(ThreadMessage.id) guarantee.
    """
    # First check if thread exists
    db_thread = crud.get_thread(db, thread_id=thread_id)
    if not db_thread:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    # Authorization: only owner or admin can read messages
    agent = crud.get_agent(db, agent_id=db_thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")

    # Fetch ORM messages and map to response schema including tool metadata
    orm_msgs = crud.get_thread_messages(db, thread_id=thread_id, skip=skip, limit=limit, include_internal=False)
    if not orm_msgs:
        return []
    result: List[ThreadMessageResponse] = []
    for m in orm_msgs:
        # Determine message_type based on role
        if m.role == "tool":
            message_type = "tool_output"
            tool_name = m.name
        elif m.role == "assistant":
            message_type = "assistant_message"
            tool_name = None
        elif m.role == "user":
            message_type = "user_message"
            tool_name = None
        else:
            # Fallback to raw role for unknown types
            message_type = f"{m.role}_message"
            tool_name = None
        result.append(
            ThreadMessageResponse(
                id=m.id,
                thread_id=m.thread_id,
                role=m.role,
                content=m.content,
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                name=m.name,
                sent_at=m.sent_at,
                processed=m.processed,
                parent_id=m.parent_id,
                message_type=message_type,
                tool_name=tool_name,
            )
        )
    return result


@router.post(
    "/{thread_id}/messages",
    response_model=ThreadMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_thread_message(
    thread_id: int, message: ThreadMessageCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)
):
    """Create a new message in a thread"""
    logger.info(f"Creating message in thread {thread_id}: role={message.role}, content={message.content}")

    # First check if thread exists
    db_thread = crud.get_thread(db, thread_id=thread_id)
    if not db_thread:
        logger.warning(f"Thread {thread_id} not found when creating message")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    # Authorization: only owner or admin can post messages
    agent = crud.get_agent(db, agent_id=db_thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")

    # Create the message (note: by default, processed=False for user messages)
    new_message = crud.create_thread_message(
        db=db,
        thread_id=thread_id,
        role=message.role,
        content=message.content,
        sent_at=message.sent_at,
    )
    logger.info(f"Created message with ID {new_message.id} in thread {thread_id}, processed={new_message.processed}")

    return new_message


@router.post("/{thread_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_thread(thread_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Process any unprocessed messages in the thread and stream back the result."""

    # Enforce per-user daily run cap (non-admins are restricted)
    assert_can_start_run(db, user=current_user)

    # Validate thread & agent and ownership
    thread = crud.get_thread(db, thread_id=thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    agent = crud.get_agent(db, agent_id=thread.agent_id)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not thread owner")

    messages = crud.get_unprocessed_messages(db, thread_id=thread_id)
    if not messages:
        return {"status": "No unprocessed messages"}

    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    runner = AgentRunner(agent)

    # User-scoped topic for ALL streaming events
    user_id = current_user.id
    topic = f"user:{user_id}"

    # Set user_id context for token streaming
    set_current_user_id(user_id)

    try:
        # Notify start of (non token) stream
        stream_start_data = StreamStartData(thread_id=thread_id)
        envelope = Envelope.create(
            message_type="stream_start",
            topic=topic,
            data=stream_start_data.model_dump(),
        )
        await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        # Execute the agent turn and record run history/events
        created_rows = await execute_thread_run_with_history(db=db, agent=agent, thread=thread, runner=runner, trigger="chat")

        # We maintain a single *stream* sequence for the entire agent turn so the
        # frontend can group chunks under one progress indicator.

        for row in created_rows:
            if row.role == "assistant":
                if runner.enable_token_stream:
                    # Phase-2: emit the new *assistant_id* frame so the frontend
                    # can link upcoming tool_output chunks to this assistant
                    # bubble while streaming is still in progress.
                    assistant_id_data = AssistantIdData(
                        thread_id=thread_id,
                        message_id=row.id,
                    )
                    envelope = Envelope.create(
                        message_type="assistant_id",
                        topic=topic,
                        data=assistant_id_data.model_dump(),
                    )
                    await topic_manager.broadcast_to_topic(topic, envelope.model_dump())
                else:
                    # Non-token mode: keep sending the full assistant_message
                    chunk_data = StreamChunkData(
                        thread_id=thread_id,
                        content=row.content,
                        chunk_type="assistant_message",
                        tool_name=None,
                        tool_call_id=None,
                        message_id=row.id,
                    )
                    envelope = Envelope.create(
                        message_type="stream_chunk",
                        topic=topic,
                        data=chunk_data.model_dump(),
                    )
                    await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

            elif row.role == "tool":
                chunk_data = StreamChunkData(
                    thread_id=thread_id,
                    content=row.content,
                    chunk_type="tool_output",
                    tool_name=getattr(row, "name", None),
                    tool_call_id=getattr(row, "tool_call_id", None),
                    message_id=row.id,
                )
                envelope = Envelope.create(
                    message_type="stream_chunk",
                    topic=topic,
                    data=chunk_data.model_dump(),
                )
                await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        # Close the stream sequence at the end
        stream_end_data = StreamEndData(thread_id=thread_id)
        envelope = Envelope.create(
            message_type="stream_end",
            topic=topic,
            data=stream_end_data.model_dump(),
        )
        await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        return {"status": "ok"}
    finally:
        # Always clean up user context
        set_current_user_id(None)
