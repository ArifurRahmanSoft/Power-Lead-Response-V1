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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SenderAccount(Base):
    __tablename__ = "t_sender_account"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "sender_email",
            name="uq_t_sender_account_tenant_email",
        ),
        CheckConstraint(
            "provider IN ('gmail', 'custom_smtp')",
            name="ck_t_sender_account_provider",
        ),
        CheckConstraint(
            "authentication_type IN ('app_password', 'password')",
            name="ck_t_sender_account_authentication_type",
        ),
        CheckConstraint(
            "encryption IN ('STARTTLS', 'TLS')",
            name="ck_t_sender_account_encryption",
        ),
        CheckConstraint(
            "connection_status IN ('not_tested', 'success', 'failed')",
            name="ck_t_sender_account_connection_status",
        ),
        CheckConstraint(
            "smtp_port BETWEEN 1 AND 65535",
            name="ck_t_sender_account_smtp_port",
        ),
        Index("ix_t_sender_account_tenant_id", "tenant_id"),
        Index("ix_t_sender_account_tenant_active", "tenant_id", "is_active"),
        Index(
            "uq_t_sender_account_default_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default = true"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    sender_email: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    authentication_type: Mapped[str] = mapped_column(String(30), nullable=False)
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False)
    encryption: Mapped[str] = mapped_column(String(20), nullable=False)
    smtp_username: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    connection_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="not_tested",
        server_default="not_tested",
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_test_message: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
