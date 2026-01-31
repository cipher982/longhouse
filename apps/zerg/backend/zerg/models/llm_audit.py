from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID


class LLMAuditLog(Base):
    __tablename__ = "llm_audit_log"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, server_default=func.now())

    # Correlation - ON DELETE SET NULL to preserve audit data when parent entities are deleted
    run_id = Column(Integer, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    commis_id = Column(String(100), nullable=True, index=True)
    thread_id = Column(Integer, ForeignKey("threads.id", ondelete="SET NULL"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Tracing - for end-to-end debugging
    # GUID: UUID for Postgres, CHAR(36) for SQLite
    trace_id = Column(GUID(), nullable=True, index=True)  # End-to-end trace correlation
    span_id = Column(GUID(), nullable=True)  # Unique ID for this LLM call

    # Request
    phase = Column(String(50))  # initial, tool_iteration, synthesis
    model = Column(String(100))
    messages = Column(JSON().with_variant(JSONB, "postgresql"))  # Full messages array
    message_count = Column(Integer)
    input_tokens = Column(Integer)

    # Response
    response_content = Column(Text)
    response_tool_calls = Column(JSON().with_variant(JSONB, "postgresql"))
    output_tokens = Column(Integer)
    reasoning_tokens = Column(Integer)

    # Timing
    duration_ms = Column(Integer)

    # Debug
    checkpoint_id = Column(String(100))
    error = Column(Text)
