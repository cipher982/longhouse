"""Trigger model for event-driven agent execution."""

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class Trigger(Base):
    """A trigger that can fire an agent (e.g. via webhook).

    Currently only the *webhook* type is implemented.  Each trigger owns a
    unique secret token that must be supplied when the webhook is invoked.
    """

    __tablename__ = "triggers"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)

    # For now we only support webhook triggers but leave room for future
    # extension (e.g. kafka, email, slack, etc.).
    type = Column(String, default="webhook", nullable=False)

    # Shared secret that must accompany incoming webhook calls.  Very simple
    # scheme for now – a random hex string.
    secret = Column(String, nullable=False, unique=True, index=True)

    # Optional JSON blob with trigger-specific configuration.  This keeps the
    # model forward-compatible so new trigger types (e.g. *email*, *slack*)
    # can persist arbitrary settings without schema migrations.  For webhook
    # triggers the column is generally **NULL**.
    config = Column(MutableDict.as_mutable(JSON), nullable=True)

    # -------------------------------------------------------------------
    # Typed *config* accessor
    # -------------------------------------------------------------------

    @property
    def config_obj(self):  # noqa: D401 – typed accessor
        """Return a :class:`TriggerConfig` parsed from ``config`` JSON.

        No caching / fallback logic – we simply construct a new model every
        call because the cost is negligible and keeps the implementation
        straightforward.
        """

        from zerg.models.trigger_config import TriggerConfig  # local import

        return TriggerConfig(**(self.config or {}))  # type: ignore[arg-type]

    def set_config_obj(self, cfg):  # noqa: D401 – mutator, TriggerConfig param
        """Assign *cfg* and persist its dict representation."""

        # Persist as raw dict so DB schema remains unchanged
        raw = cfg.model_dump()  # type: ignore[attr-defined]

        self.config = raw  # type: ignore[assignment]

    created_at = Column(DateTime, server_default=func.now())

    # ORM relationships
    agent = relationship("Agent", backref="triggers")
