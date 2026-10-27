import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LeadActivity(Base):
    __tablename__ = "t_lead_activity"
    __table_args__ = (
        Index(
            "ix_t_lead_activity_tenant_lead_timeline",
            "tenant_id",
            "lead_id",
            "occurred_at",
        ),
        Index("ix_t_lead_activity_performed_by", "performed_by"),
        Index("ix_t_lead_activity_related_activity_id", "related_activity_id"),
        CheckConstraint(
            "activity_type IN ('initial_contact', 'follow_up', 'inbound_reply', 'note')",
            name="ck_t_lead_activity_type",
        ),
        CheckConstraint(
            "channel IN ('email', 'phone', 'sms', 'whatsapp', 'other')",
            name="ck_t_lead_activity_channel",
        ),
        CheckConstraint(
            "direction IN ('outbound', 'inbound', 'internal')",
            name="ck_t_lead_activity_direction",
        ),
        CheckConstraint(
            "related_activity_id IS NULL OR related_activity_id <> id",
            name="ck_t_lead_activity_not_self_related",
        ),
        CheckConstraint(
            "NOT is_system OR NOT is_amendment",
            name="ck_t_lead_activity_system_not_amendment",
        ),
        CheckConstraint(
            "contact_stage IS NULL OR contact_stage IN "
            "('initial', 'follow_up', 'second_follow_up', 'final_follow_up')",
            name="ck_t_lead_activity_contact_stage",
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
    activity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    performed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    outcome: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_lead_activity.id", ondelete="RESTRICT"),
        nullable=True,
    )
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_amendment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    externally_performed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    provider_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    amendment_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_outbound_message.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    sms_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_sms_message.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    whatsapp_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_message.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    contact_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
