import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SyncSource(enum.StrEnum):
    plane = "plane"
    github = "github"


class WebhookSource(enum.StrEnum):
    plane = "plane"
    github = "github"


class WebhookStatus(enum.StrEnum):
    pending = "pending"
    processed = "processed"
    failed = "failed"


class CardIssueLink(Base):
    __tablename__ = "card_issue_link"
    __table_args__ = (
        UniqueConstraint("gh_repo", "gh_issue_number", name="uq_card_issue_link_repo_issue"),
    )

    plane_card_id: Mapped[str] = mapped_column(String, primary_key=True)
    plane_project_id: Mapped[str] = mapped_column(String, nullable=False)
    gh_repo: Mapped[str] = mapped_column(String, nullable=False)
    gh_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    gh_issue_node_id: Mapped[str] = mapped_column(String, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sync_source_last: Mapped[SyncSource] = mapped_column(
        Enum(SyncSource, name="sync_source"), nullable=False
    )


class RepoModuleMap(Base):
    __tablename__ = "repo_module_map"

    plane_module_id: Mapped[str] = mapped_column(String, primary_key=True)
    plane_project_id: Mapped[str] = mapped_column(String, nullable=False)
    gh_repo: Mapped[str] = mapped_column(String, nullable=False)


class LabelMap(Base):
    __tablename__ = "label_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plane_project_id: Mapped[str] = mapped_column(String, nullable=False)
    plane_label_id: Mapped[str] = mapped_column(String, nullable=False)
    gh_repo: Mapped[str] = mapped_column(String, nullable=False)
    gh_label: Mapped[str] = mapped_column(String, nullable=False)


class UserMap(Base):
    __tablename__ = "user_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plane_user_id: Mapped[str] = mapped_column(String, nullable=False)
    gh_login: Mapped[str] = mapped_column(String, nullable=False)
    discord_user_id: Mapped[str | None] = mapped_column(String, nullable=True)


class PrNotificationState(Base):
    __tablename__ = "pr_notification_state"

    pr_node_id: Mapped[str] = mapped_column(String, primary_key=True)
    gh_repo: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ready_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_ready_cycle_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    discord_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    discord_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)


class WebhookEventLog(Base):
    __tablename__ = "webhook_event_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[WebhookSource] = mapped_column(
        Enum(WebhookSource, name="webhook_source"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[WebhookStatus] = mapped_column(
        Enum(WebhookStatus, name="webhook_status"), nullable=False
    )
