# models.py
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

    # Relationships
    email_accounts = relationship("EmailAccount", back_populates="user")
    # New: user ↔ tasks
    tasks = relationship(
        "Task",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

class UserSetting(Base):
    __tablename__ = "user_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # e.g. "agent_greeting", "gmail_summary_enabled", "gmail_account_id"
    key = Column(String, nullable=False)
    value = Column(JSONB, nullable=False, default=dict)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="settings")


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    provider_type = Column(String, nullable=False)          # "gmail", "imap_custom", etc.
    oauth_provider = Column(String, nullable=True)
    oauth_access_token = Column(Text, nullable=True)
    oauth_refresh_token = Column(Text, nullable=True)
    oauth_expires_at = Column(DateTime, nullable=True)

    imap_host = Column(String, nullable=True)
    imap_port = Column(Integer, nullable=True)
    imap_ssl = Column(Boolean, nullable=True)
    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_ssl = Column(Boolean, nullable=True)
    username = Column(String, nullable=True)
    password_enc = Column(Text, nullable=True)

    email_address = Column(String, nullable=False)
    display_name = Column(String, nullable=True)

    is_primary = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="email_accounts")


# =========================
# Task Engine Models
# =========================

class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    COLLECTING_INPUT = "COLLECTING_INPUT"
    READY = "READY"
    EXECUTING = "EXECUTING"
    WAITING = "WAITING"
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

    type = Column(SAEnum(TaskType, name="task_type_enum"), nullable=False)
    status = Column(SAEnum(TaskStatus, name="task_status_enum"), nullable=False, default=TaskStatus.PENDING)

    # Flexible JSON structures used by the task engine
    # {
    #   "required": [...],
    #   "optional": {...},
    #   "collected": {...}
    # }
    inputs = Column(JSONB, nullable=False, default=dict)

    # {
    #   "cursor": "step_name_or_index",
    #   "context": {...},
    #   "history": [...]
    # }
    state = Column(JSONB, nullable=False, default=dict)

    # {
    #   "result": ...,
    #   "error": "..."
    # }
    execution = Column(JSONB, nullable=False, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="tasks")
