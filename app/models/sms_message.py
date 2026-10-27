import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


CONTACT_TYPES = "'initial', 'follow_up', 'second_follow_up', 'final_follow_up'"
SMS_STATES = (
    "'accepted', 'queued', 'sent', 'delivered', 'failed', "
    "'undelivered', 'unknown', 'skipped'"
)


class SmsBatch(Base):
    __tablename__ = "t_sms_batch"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_t_sms_batch_tenant_idempotency"
        ),
        CheckConstraint(
            f"contact_type IN ({CONTACT_TYPES})", name="ck_t_sms_batch_contact_type"
        ),
        CheckConstraint(
            "status IN ('processing', 'completed', 'partially_completed')",
            name="ck_t_sms_batch_status",
        ),
        Index("ix_t_sms_batch_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    message_template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_message_template.id", ondelete="RESTRICT"),
        nullable=False,
    )
    template_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    contact_type: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="processing", server_default="processing"
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SmsMessage(Base):
    __tablename__ = "t_sms_message"
    __table_args__ = (
        UniqueConstraint("batch_id", "lead_id", name="uq_t_sms_message_batch_lead"),
        UniqueConstraint("provider_message_sid", name="uq_t_sms_message_provider_sid"),
        CheckConstraint(
            f"contact_type IN ({CONTACT_TYPES})", name="ck_t_sms_message_contact_type"
        ),
        CheckConstraint(f"status IN ({SMS_STATES})", name="ck_t_sms_message_status"),
        Index("ix_t_sms_message_tenant_lead_created", "tenant_id", "lead_id", "created_at"),
        Index("ix_t_sms_message_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_sms_batch.id", ondelete="RESTRICT"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="RESTRICT"), nullable=False
    )
    message_template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_message_template.id", ondelete="RESTRICT"),
        nullable=False,
    )
    template_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    template_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    template_body_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sender_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    contact_type: Mapped[str] = mapped_column(String(30), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown", server_default="unknown"
    )
    provider_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SmsBatchRecipient(Base):
    __tablename__ = "t_sms_batch_recipient"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "requested_lead_id", name="uq_t_sms_batch_recipient_requested"
        ),
        UniqueConstraint("sms_message_id", name="uq_t_sms_batch_recipient_message"),
        CheckConstraint(
            f"status IN ({SMS_STATES})", name="ck_t_sms_batch_recipient_status"
        ),
        Index("ix_t_sms_batch_recipient_batch", "batch_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_sms_batch.id", ondelete="CASCADE"), nullable=False
    )
    requested_lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="RESTRICT"), nullable=True
    )
    sms_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_sms_message.id", ondelete="RESTRICT"), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class SmsMessageEvent(Base):
    __tablename__ = "t_sms_message_event"
    __table_args__ = (
        Index("ix_t_sms_message_event_message_time", "sms_message_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    sms_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_sms_message.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    safe_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
