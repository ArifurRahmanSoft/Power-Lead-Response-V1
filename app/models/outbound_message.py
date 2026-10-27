import uuid
from datetime import datetime
from typing import Any

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


MESSAGE_STATES = "'queued', 'processing', 'sent', 'failed', 'unknown', 'cancelled', 'skipped'"
CONTACT_STAGES = "'initial', 'follow_up', 'second_follow_up', 'final_follow_up'"


class OutboundMessageBatch(Base):
    __tablename__ = "t_outbound_message_batch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_t_outbound_batch_idempotency"),
        CheckConstraint("selection_mode IN ('selected', 'all_matching')", name="ck_t_outbound_batch_selection_mode"),
        CheckConstraint(f"contact_stage IN ({CONTACT_STAGES})", name="ck_t_outbound_batch_stage"),
        CheckConstraint("status IN ('previewed', 'queued', 'processing', 'completed', 'partially_completed', 'cancelled')", name="ck_t_outbound_batch_status"),
        Index("ix_t_outbound_batch_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False)
    sender_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_sender_account.id", ondelete="RESTRICT"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template.id", ondelete="RESTRICT"), nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template_version.id", ondelete="RESTRICT"), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    contact_stage: Mapped[str] = mapped_column(String(30), nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    selection_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="previewed", server_default="previewed")
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True)


class OutboundBatchRecipient(Base):
    __tablename__ = "t_outbound_batch_recipient"
    __table_args__ = (
        UniqueConstraint("batch_id", "requested_lead_id", name="uq_t_outbound_batch_recipient_lead"),
        CheckConstraint("preview_status IN ('eligible', 'blocked', 'conflict')", name="ck_t_outbound_batch_recipient_preview_status"),
        Index("ix_t_outbound_batch_recipient_batch", "batch_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message_batch.id", ondelete="CASCADE"), nullable=False)
    requested_lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="RESTRICT"), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preview_status: Mapped[str] = mapped_column(String(20), nullable=False)
    exclusion_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    preview_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message_preview.id", ondelete="RESTRICT"), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message.id", ondelete="RESTRICT"), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OutboundMessagePreview(Base):
    __tablename__ = "t_outbound_message_preview"
    __table_args__ = (
        Index("ix_t_outbound_preview_tenant_creator", "tenant_id", "created_by", "expires_at"),
        CheckConstraint("context_type IN ('lead', 'customer')", name="ck_t_outbound_preview_context"),
        CheckConstraint(
            "purpose IN ('lead_acquisition', 'customer_service')",
            name="ck_t_outbound_preview_purpose",
        ),
        CheckConstraint(
            "(context_type = 'lead' AND lead_id IS NOT NULL AND customer_id IS NULL) OR "
            "(context_type = 'customer' AND customer_id IS NOT NULL AND lead_id IS NOT NULL)",
            name="ck_t_outbound_preview_context_refs",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False)
    context_type: Mapped[str] = mapped_column(String(20), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="RESTRICT"), nullable=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_customer.id", ondelete="RESTRICT"), nullable=True)
    sender_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_sender_account.id", ondelete="RESTRICT"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template.id", ondelete="RESTRICT"), nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template_version.id", ondelete="RESTRICT"), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    asset_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboundMessage(Base):
    __tablename__ = "t_outbound_message"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_t_outbound_message_tenant_idempotency"),
        Index("ix_t_outbound_message_tenant_created", "tenant_id", "created_at"),
        Index("ix_t_outbound_message_lead", "tenant_id", "lead_id", "created_at"),
        Index("ix_t_outbound_message_customer", "tenant_id", "customer_id", "created_at"),
        Index(
            "uq_t_outbound_active_lead",
            "tenant_id",
            "lead_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'processing')"),
            sqlite_where=text("status IN ('queued', 'processing')"),
        ),
        CheckConstraint(f"status IN ({MESSAGE_STATES})", name="ck_t_outbound_message_status"),
        CheckConstraint("context_type IN ('lead', 'customer')", name="ck_t_outbound_message_context"),
        CheckConstraint("purpose IN ('lead_acquisition', 'customer_service')", name="ck_t_outbound_message_purpose"),
        CheckConstraint(f"contact_stage IN ({CONTACT_STAGES})", name="ck_t_outbound_message_stage"),
        CheckConstraint(
            "(context_type = 'lead' AND lead_id IS NOT NULL AND customer_id IS NULL) OR "
            "(context_type = 'customer' AND customer_id IS NOT NULL AND lead_id IS NOT NULL)",
            name="ck_t_outbound_message_context_refs",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    preview_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message_preview.id", ondelete="RESTRICT"), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message_batch.id", ondelete="RESTRICT"), nullable=True)
    contact_stage: Mapped[str] = mapped_column(String(30), nullable=False, default="initial", server_default="initial")
    context_type: Mapped[str] = mapped_column(String(20), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="RESTRICT"), nullable=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_customer.id", ondelete="RESTRICT"), nullable=True)
    sender_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_sender_account.id", ondelete="RESTRICT"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template.id", ondelete="RESTRICT"), nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_mail_template_version.id", ondelete="RESTRICT"), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    asset_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboundMessageJob(Base):
    __tablename__ = "t_outbound_message_job"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_t_outbound_message_job_message"),
        Index("ix_t_outbound_job_claim", "status", "available_at", "lease_expires_at"),
        CheckConstraint("status IN ('queued', 'processing', 'retry', 'completed', 'failed', 'unknown', 'cancelled')", name="ck_t_outbound_job_status"),
        CheckConstraint("attempts >= 0", name="ck_t_outbound_job_attempts_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OutboundMessageEvent(Base):
    __tablename__ = "t_outbound_message_event"
    __table_args__ = (Index("ix_t_outbound_event_message_time", "message_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_outbound_message.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True)
    safe_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
