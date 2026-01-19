"""Waitlist model for collecting email signups before launch."""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.sql import func

from zerg.database import Base


class WaitlistEntry(Base):
    """Waitlist signup entry.

    Collects email signups for features not yet available (e.g., Pro tier).
    """

    __tablename__ = "waitlist_entries"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False, unique=True, index=True)
    source = Column(String(50), nullable=False, default="pricing_pro")  # Where they signed up
    notes = Column(Text, nullable=True)  # Optional notes from user
    created_at = Column(DateTime, server_default=func.now())
