from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from control_plane.db import Base


class User(Base):
    __tablename__ = "cp_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    instance: Mapped["Instance"] = relationship("Instance", back_populates="user", uselist=False)


class Instance(Base):
    __tablename__ = "cp_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("cp_users.id"), unique=True)

    subdomain: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    container_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="provisioning")

    data_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # Version tracking (rolling deploy)
    current_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_healthy_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_ring: Mapped[int] = mapped_column(Integer, default=2, server_default="2")

    # Deploy state
    deploy_state: Mapped[str] = mapped_column(String(32), default="idle", server_default="idle")
    deploy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deploy_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deploy_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="instance")


class Deployment(Base):
    __tablename__ = "cp_deployments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    image: Mapped[str] = mapped_column(Text)
    image_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    rings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    max_parallel: Mapped[int] = mapped_column(Integer, default=5)
    failure_threshold: Mapped[int] = mapped_column(Integer, default=3)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
