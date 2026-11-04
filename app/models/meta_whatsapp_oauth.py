import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MetaWhatsAppOAuthOperation(Base):
    __tablename__ = "t_meta_whatsapp_oauth_operation"
    __table_args__ = (
        Index("ix_t_meta_whatsapp_oauth_operation_expiry", "expires_at"),
        Index("ix_t_meta_whatsapp_oauth_operation_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_whatsapp_channel.id", ondelete="SET NULL"),
        nullable=True,
    )
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    configuration_id: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_http_status: Mapped[int | None] = mapped_column(nullable=True)
    discovered_business_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discovered_waba_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discovered_phone_number_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discovered_sender_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    discovered_display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    connection_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscription_succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
