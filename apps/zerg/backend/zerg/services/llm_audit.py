import asyncio
import logging
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List

from sqlalchemy.orm import Session

from zerg.database import get_session_factory
from zerg.models.llm_audit import LLMAuditLog

logger = logging.getLogger(__name__)


def serialize_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    """Serialize LangChain messages to a JSON-safe format."""
    serialized = []
    for i, msg in enumerate(messages):
        msg_dict = {
            "index": i,
            "type": type(msg).__name__,
            "role": getattr(msg, "type", "unknown"),
        }

        # Get content
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            msg_dict["content"] = content
        elif isinstance(content, list):
            # Handle multimodal content (list of dicts)
            msg_dict["content"] = content
        else:
            msg_dict["content"] = str(content) if content else None

        # Get tool calls if present
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            msg_dict["tool_calls"] = tool_calls

        # Get tool_call_id if present (for ToolMessage)
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id:
            msg_dict["tool_call_id"] = tool_call_id

        # Get name if present (for ToolMessage)
        name = getattr(msg, "name", None)
        if name:
            msg_dict["name"] = name

        serialized.append(msg_dict)
    return serialized


class LLMAuditLogger:
    """Async audit logger for LLM interactions."""

    def __init__(self):
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._started = False

    def ensure_started(self):
        """Start background writer task if not running."""
        if not self._started:
            # We use a flag to avoid checking loop state repeatedly
            # However, we must ensure we are in a running loop
            try:
                loop = asyncio.get_running_loop()
                if self._task is None or self._task.done():
                    self._task = loop.create_task(self._writer_loop())
                    self._started = True
            except RuntimeError:
                # No running loop, can't start yet
                pass

    async def log_request(
        self,
        *,
        run_id: int | None,
        worker_id: str | None,
        thread_id: int | None = None,
        owner_id: int | None = None,
        trace_id: str | None = None,
        phase: str,
        model: str,
        messages: List[Any],
    ) -> str:
        """Log an LLM request. Returns correlation ID for response matching."""
        self.ensure_started()

        correlation_id = f"{datetime.utcnow().isoformat()}_{phase}_{id(messages)}"
        # Generate a unique span_id for this LLM call
        span_id = str(uuid.uuid4())

        try:
            serialized_messages = serialize_messages(messages)

            payload = {
                "type": "request",
                "correlation_id": correlation_id,
                "run_id": run_id,
                "worker_id": worker_id,
                "thread_id": thread_id,
                "owner_id": owner_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "phase": phase,
                "model": model,
                "messages": serialized_messages,
                "message_count": len(messages),
                "created_at": datetime.utcnow(),
            }

            # Non-blocking put with maxsize protection
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("LLM Audit queue full, dropping request log")
        except Exception as e:
            logger.warning(f"Failed to log LLM request: {e}")

        return correlation_id

    async def log_response(
        self,
        correlation_id: str,
        *,
        content: Any | None,
        tool_calls: List[dict] | None,
        input_tokens: int | None,
        output_tokens: int | None,
        reasoning_tokens: int | None,
        duration_ms: int,
        error: str | None = None,
    ):
        """Log an LLM response."""
        self.ensure_started()

        try:
            # Normalize content
            response_content = None
            if isinstance(content, str):
                response_content = content
            elif isinstance(content, list):
                response_content = str(content)  # JSONB or text? Model has Text.
                # Actually spec says Text for response_content.
                # If it's a list (multimodal), we might want to json dump it or just str()
            elif content is not None:
                response_content = str(content)

            payload = {
                "type": "response",
                "correlation_id": correlation_id,
                "response_content": response_content,
                "response_tool_calls": tool_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "duration_ms": duration_ms,
                "error": error,
            }

            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("LLM Audit queue full, dropping response log")
        except Exception as e:
            logger.warning(f"Failed to log LLM response: {e}")

    async def _writer_loop(self):
        """Background task that batches writes to DB."""
        # Use local import for DB session factory to avoid circular deps or init issues
        SessionLocal = get_session_factory()

        pending: Dict[str, dict] = {}  # correlation_id -> partial record

        while True:
            try:
                # Collect items with timeout for batching
                items = []
                try:
                    # Wait for first item
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    items.append(item)

                    # Drain queue up to 100 items
                    while len(items) < 100:
                        try:
                            item = self._queue.get_nowait()
                            items.append(item)
                        except asyncio.QueueEmpty:
                            break
                except asyncio.TimeoutError:
                    pass

                if not items:
                    continue

                # Process items
                for item in items:
                    cid = item.get("correlation_id")
                    if not cid:
                        continue

                    if item["type"] == "request":
                        pending[cid] = item
                    elif item["type"] == "response":
                        if cid in pending:
                            pending[cid].update(item)
                        else:
                            # Orphaned response? (e.g. restart happened)
                            # Or we processed request in previous batch but response came later?
                            # For simple audit logging, we assume request and response come close together.
                            # If response comes much later, 'pending' might still have it if we didn't flush.
                            # But we flush completed records below.
                            # So if we flushed request, and now get response, we need to UPDATE the DB row.
                            # The current design assumes we hold the request in memory until response arrives.
                            # This is risky for long streams.
                            # But wait, the spec says: "pending: dict[str, dict] = {} # correlation_id -> partial record"
                            # And "Write completed records ... if 'response_content' in record or 'error' in record"
                            # This implies we HOLD the request until the response arrives.
                            # LLM calls are usually sub-minute.
                            pass

                # Write completed records
                to_write = []
                # Check for completed items or items that have timed out?
                # For now, just write what has response/error

                completed_cids = []
                for cid, record in pending.items():
                    if "response_content" in record or "error" in record:
                        to_write.append(record)
                        completed_cids.append(cid)

                # Cleanup pending
                for cid in completed_cids:
                    del pending[cid]

                if to_write:
                    session = SessionLocal()
                    try:
                        for record in to_write:
                            # Convert trace_id and span_id to UUID objects if present
                            trace_id_uuid = uuid.UUID(record["trace_id"]) if record.get("trace_id") else None
                            span_id_uuid = uuid.UUID(record["span_id"]) if record.get("span_id") else None

                            session.add(
                                LLMAuditLog(
                                    run_id=record.get("run_id"),
                                    worker_id=record.get("worker_id"),
                                    thread_id=record.get("thread_id"),
                                    owner_id=record.get("owner_id"),
                                    trace_id=trace_id_uuid,
                                    span_id=span_id_uuid,
                                    phase=record.get("phase"),
                                    model=record.get("model"),
                                    messages=record.get("messages"),
                                    message_count=record.get("message_count"),
                                    input_tokens=record.get("input_tokens"),
                                    response_content=record.get("response_content"),
                                    response_tool_calls=record.get("response_tool_calls"),
                                    output_tokens=record.get("output_tokens"),
                                    reasoning_tokens=record.get("reasoning_tokens"),
                                    duration_ms=record.get("duration_ms"),
                                    error=record.get("error"),
                                    created_at=record.get("created_at"),
                                )
                            )
                        session.commit()
                    except Exception as e:
                        logger.error(f"Error writing audit logs to DB: {e}")
                        session.rollback()
                    finally:
                        session.close()

                # Prune stale pending requests (> 10 mins old) to prevent memory leak
                # if response never arrives (crash, cancel, timeout)
                stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
                stale_cids = []
                for cid, record in pending.items():
                    created_at = record.get("created_at")
                    if created_at and isinstance(created_at, datetime):
                        # Make created_at timezone-aware if needed
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if created_at < stale_cutoff:
                            stale_cids.append(cid)
                if stale_cids:
                    logger.warning(f"Pruning {len(stale_cids)} stale LLM audit pending entries (> 10 min old)")
                    for cid in stale_cids:
                        del pending[cid]

            except asyncio.CancelledError:
                # Shutdown
                break
            except Exception as e:
                logger.exception(f"Audit writer error: {e}")
                await asyncio.sleep(1)


def query_audit_log(
    db: Session,
    *,
    run_id: int | None = None,
    worker_id: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> List[LLMAuditLog]:
    """Query audit logs with filters."""
    query = db.query(LLMAuditLog)

    if run_id:
        query = query.filter(LLMAuditLog.run_id == run_id)
    if worker_id:
        query = query.filter(LLMAuditLog.worker_id == worker_id)
    if since:
        query = query.filter(LLMAuditLog.created_at >= since)

    return query.order_by(LLMAuditLog.created_at.desc()).limit(limit).all()


def get_run_llm_history(db: Session, run_id: int) -> List[Dict]:
    """Get full LLM interaction history for a run (for debugging)."""
    logs = query_audit_log(db, run_id=run_id, limit=1000)
    return [
        {
            "phase": log.phase,
            "model": log.model,
            "messages": log.messages,
            "response": {
                "content": log.response_content,
                "tool_calls": log.response_tool_calls,
            },
            "tokens": {
                "input": log.input_tokens,
                "output": log.output_tokens,
                "reasoning": log.reasoning_tokens,
            },
            "duration_ms": log.duration_ms,
            "error": log.error,
            "created_at": log.created_at.isoformat(),
        }
        for log in reversed(logs)
    ]


# Global instance
audit_logger = LLMAuditLogger()
