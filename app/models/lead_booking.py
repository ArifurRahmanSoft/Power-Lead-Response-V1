import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class LeadBooking(Base):
    __tablename__ = "t_lead_booking"
    __table_args__ = (
        Index("ix_t_lead_booking_tenant_lead_scheduled", "tenant_id", "lead_id", "scheduled_at"),
        Index("ix_t_lead_booking_responsible_status", "responsible_user_id", "status"),
        CheckConstraint(
            "source IN ('manual', 'email', 'phone', 'sms', 'whatsapp', 'other')",
            name="ck_t_lead_booking_source",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'confirmed', 'cancelled', 'completed')",
            name="ck_t_lead_booking_status",
        ),
        CheckConstraint("row_version >= 1", name="ck_t_lead_booking_row_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    responsible_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
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
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class LeadBookingEvent(Base):
    __tablename__ = "t_lead_booking_event"
    __table_args__ = (
        Index("ix_t_lead_booking_event_tenant_booking", "tenant_id", "booking_id", "recorded_at"),
        CheckConstraint(
            "event_type IN ('created', 'confirmed', 'cancelled', 'rescheduled')",
            name="ck_t_lead_booking_event_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead_booking.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LeadAutomationStopEvent(Base):
    __tablename__ = "t_lead_automation_stop_event"
    __table_args__ = (
        Index(
            "ix_t_lead_automation_stop_tenant_lead_key",
            "tenant_id",
            "lead_id",
            "stop_key",
            "recorded_at",
        ),
        CheckConstraint(
            "trigger_type IN ('manual_reply', 'booking_created', 'booking_confirmed', "
            "'booking_rescheduled', 'lead_converted')",
            name="ck_t_lead_automation_stop_trigger_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    stop_key: Mapped[str] = mapped_column(
        String(80), nullable=False, default="recovery_follow_up", server_default="recovery_follow_up"
    )
    trigger_type: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
