import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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


WHATSAPP_STATUSES = (
    "'received', 'accepted', 'queued', 'sending', 'sent', 'delivered', "
    "'read', 'failed', 'undelivered', 'unknown'"
)


class WhatsAppChannel(Base):
    __tablename__ = "t_whatsapp_channel"
    __table_args__ = (
        UniqueConstraint(
            "twilio_account_sid",
            "sender_address",
            name="uq_t_whatsapp_channel_account_sender",
        ),
        UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_channel_id",
            name="uq_t_whatsapp_channel_provider_identity",
        ),
        CheckConstraint(
            "provider IN ('twilio', 'meta', 'messenger', 'instagram')",
            name="ck_t_whatsapp_channel_provider",
        ),
        Index("ix_t_whatsapp_channel_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="twilio", server_default="twilio"
    )
    provider_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_channel_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    twilio_account_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_address: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WhatsAppConversation(Base):
    __tablename__ = "t_whatsapp_conversation"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "external_address",
            name="uq_t_whatsapp_conversation_channel_contact",
        ),
        Index(
            "ix_t_whatsapp_conversation_tenant_activity",
            "tenant_id",
            "last_message_at",
        ),
        Index("ix_t_whatsapp_conversation_lead", "tenant_id", "lead_id"),
        Index("ix_t_whatsapp_conversation_customer", "tenant_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_address: Mapped[str] = mapped_column(String(32), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_customer.id", ondelete="SET NULL"), nullable=True
    )
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(240), nullable=True)
    provider_display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_profile_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_profile_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_profile_error_code: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WhatsAppMessage(Base):
    __tablename__ = "t_whatsapp_message"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_t_whatsapp_message_tenant_idempotency"
        ),
        UniqueConstraint(
            "provider",
            "provider_message_sid",
            name="uq_t_whatsapp_message_provider_identifier",
        ),
        CheckConstraint(
            "provider IN ('twilio', 'meta', 'messenger', 'instagram')",
            name="ck_t_whatsapp_message_provider",
        ),
        CheckConstraint(
            "message_mode IN ('text', 'approved_template')",
            name="ck_t_whatsapp_message_mode",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_t_whatsapp_message_direction",
        ),
        CheckConstraint(
            f"status IN ({WHATSAPP_STATUSES})",
            name="ck_t_whatsapp_message_status",
        ),
        CheckConstraint(
            "submission_state IN ('received', 'processing', 'completed')",
            name="ck_t_whatsapp_message_submission_state",
        ),
        Index(
            "ix_t_whatsapp_message_conversation_time",
            "conversation_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="twilio", server_default="twilio"
    )
    message_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="text", server_default="text"
    )
    purpose: Mapped[str | None] = mapped_column(String(40), nullable=True)
    template_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_metadata: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    sender_address: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_address: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_message_sid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    submission_state: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_request_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WhatsAppDeliveryEvent(Base):
    __tablename__ = "t_whatsapp_delivery_event"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_t_whatsapp_delivery_event_key"),
        CheckConstraint(
            "provider IN ('twilio', 'meta', 'messenger', 'instagram')",
            name="ck_t_whatsapp_delivery_event_provider",
        ),
        CheckConstraint(
            f"status IN ({WHATSAPP_STATUSES})",
            name="ck_t_whatsapp_delivery_event_status",
        ),
        Index(
            "ix_t_whatsapp_delivery_event_provider_sid",
            "tenant_id",
            "provider_message_sid",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_channel.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_message.id", ondelete="CASCADE"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="twilio", server_default="twilio"
    )
    provider_message_sid: Mapped[str] = mapped_column(String(255), nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    safe_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WhatsAppConversationRead(Base):
    __tablename__ = "t_whatsapp_conversation_read"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_conversation.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_read_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_message.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
