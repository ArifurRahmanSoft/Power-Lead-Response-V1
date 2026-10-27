import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LeadTask(Base):
    __tablename__ = "t_lead_task"
    __table_args__ = (
        Index("ix_t_lead_task_tenant_lead", "tenant_id", "lead_id"),
        Index(
            "ix_t_lead_task_tenant_assignee_status_due",
            "tenant_id",
            "assigned_user_id",
            "status",
            "due_at",
        ),
        Index("ix_t_lead_task_related_activity_id", "related_activity_id"),
        CheckConstraint(
            "task_type IN ('contact', 'follow_up', 'review', 'other')",
            name="ck_t_lead_task_type",
        ),
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_t_lead_task_priority",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')",
            name="ck_t_lead_task_status",
        ),
        CheckConstraint("row_version >= 1", name="ck_t_lead_task_row_version_positive"),
        CheckConstraint(
            "(status = 'completed' AND completed_by IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_by IS NULL AND completed_at IS NULL)",
            name="ck_t_lead_task_completion_metadata",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_by IS NULL AND cancelled_at IS NULL)",
            name="ck_t_lead_task_cancellation_metadata",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_lead.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="normal", server_default="normal"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    related_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_lead_activity.id", ondelete="RESTRICT"),
        nullable=True,
    )
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
