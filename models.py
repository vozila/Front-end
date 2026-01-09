# models.py
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
import enum

from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    Enum as SAEnum,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    email = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    settings = relationship(
        "UserSetting",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    email_accounts = relationship("EmailAccount", back_populates="user")

    tasks = relationship(
        "Task",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserSetting(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSONB, nullable=False, default=dict)

    user = relationship("User", back_populates="settings")

    __table_args__ = (
        Index("ix_user_settings_user_key", "user_id", "key", unique=True),
    )


class ProviderType(str, enum.Enum):
    gmail = "gmail"


class OAuthProvider(str, enum.Enum):
    google = "google"


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    provider_type = Column(SAEnum(ProviderType), nullable=False, default=ProviderType.gmail)
    oauth_provider = Column(SAEnum(OAuthProvider), nullable=False, default=OAuthProvider.google)

    access_token_enc = Column(Text, nullable=True)
    refresh_token_enc = Column(Text, nullable=True)

    email_address = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="email_accounts")


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class TaskType(str, enum.Enum):
    REMINDER = "REMINDER"
    TIMER = "TIMER"
    EMAIL_CHECK = "EMAIL_CHECK"
    NOTE = "NOTE"
    WORKFLOW = "WORKFLOW"
    COUNTING = "COUNTING"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    task_type = Column(SAEnum(TaskType), nullable=False)
    status = Column(SAEnum(TaskStatus), nullable=False, default=TaskStatus.PENDING)

    title = Column(String, nullable=True)

    input = Column(JSONB, nullable=False, default=dict)
    state = Column(JSONB, nullable=False, default=dict)
    execution = Column(JSONB, nullable=False, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="tasks")


# =========================
# Long-term Memory (shared DB table with backend)
# =========================

class CallerMemoryEvent(Base):
    """Mirrors backend.models.CallerMemoryEvent (caller_memory_events).

    Used by the Control Plane for admin/debug tooling only.
    """
    __tablename__ = "caller_memory_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))

    tenant_id = Column(String, index=True, nullable=False)
    caller_id = Column(String, index=True, nullable=False)
    call_sid = Column(String, index=True, nullable=True)

    kind = Column(String, index=True, nullable=False, default="event")
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    skill_key = Column(String, index=True, nullable=False)
    text = Column(Text, nullable=False)

    data_json = Column(JSONB, nullable=True)
    tags_json = Column(JSONB, nullable=True)


Index(
    "ix_mem_tenant_caller_created",
    CallerMemoryEvent.tenant_id,
    CallerMemoryEvent.caller_id,
    CallerMemoryEvent.created_at.desc(),
)
Index(
    "ix_mem_tenant_caller_skill_created",
    CallerMemoryEvent.tenant_id,
    CallerMemoryEvent.caller_id,
    CallerMemoryEvent.skill_key,
    CallerMemoryEvent.created_at.desc(),
)
