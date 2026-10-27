import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LeadAssignmentHistory(Base):
    __tablename__ = "t_lead_assignment_history"
    __table_args__ = (
        Index(
            "ix_t_lead_assignment_history_tenant_lead",
            "tenant_id",
            "lead_id",
            "assigned_at",
        ),
        CheckConstraint(
            "task_disposition IN ('transfer', 'retain', 'cancel')",
            name="ck_t_lead_assignment_task_disposition",
        ),
        CheckConstraint(
            "affected_task_count >= 0",
            name="ck_t_lead_assignment_affected_task_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    previous_assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    new_assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    task_disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    affected_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lead_row_version: Mapped[int] = mapped_column(Integer, nullable=False)
