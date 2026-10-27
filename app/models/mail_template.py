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


class MailTemplate(Base):
    __tablename__ = "t_mail_template"
    __table_args__ = (
        Index("ix_t_mail_template_tenant_id", "tenant_id"),
        Index(
            "uq_t_mail_template_active_name",
            "tenant_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
            sqlite_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )


class MailTemplateVersion(Base):
    __tablename__ = "t_mail_template_version"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_t_mail_template_version_number"),
        CheckConstraint(
            "approval_status IN ('draft', 'approved')",
            name="ck_t_mail_template_version_approval_status",
        ),
        Index("ix_t_mail_template_version_tenant_template", "tenant_id", "template_id"),
        Index(
            "uq_t_mail_template_single_draft",
            "template_id",
            unique=True,
            postgresql_where=text("approval_status = 'draft'"),
            sqlite_where=text("approval_status = 'draft'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_mail_template.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    signature_html: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    signature_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    approval_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MailTemplateAsset(Base):
    __tablename__ = "t_mail_template_asset"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('inline_body', 'signature_image', 'attachment')",
            name="ck_t_mail_template_asset_kind",
        ),
        Index("ix_t_mail_template_asset_tenant_id", "tenant_id"),
        Index("ix_t_mail_template_asset_sha256", "tenant_id", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MailTemplateVersionAsset(Base):
    __tablename__ = "t_mail_template_version_asset"

    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_mail_template_version.id", ondelete="CASCADE"),
        primary_key=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_mail_template_asset.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
