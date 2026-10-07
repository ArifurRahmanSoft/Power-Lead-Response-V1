import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Lead(Base):
    __tablename__ = "t_lead"
    __table_args__ = (
        UniqueConstraint("tracking_id", name="uq_t_lead_tracking_id"),
        Index("ix_t_lead_tenant_id", "tenant_id"),
        Index("ix_t_lead_assigned_user_id", "assigned_user_id"),
        Index("ix_t_lead_import_batch_id", "import_batch_id"),
        Index(
            "uq_t_lead_active_tenant_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("row_version >= 1", name="ck_t_lead_row_version_positive"),
        CheckConstraint("revenue_amount IS NULL OR revenue_amount >= 0", name="ck_t_lead_revenue_nonnegative"),
        CheckConstraint(
            "(revenue_amount IS NULL AND revenue_currency IS NULL AND revenue_period IS NULL) OR "
            "(revenue_amount IS NOT NULL AND revenue_currency IS NOT NULL AND revenue_period IS NOT NULL)",
            name="ck_t_lead_revenue_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    tracking_id: Mapped[str] = mapped_column(String(40), nullable=False)
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_original: Mapped[str | None] = mapped_column(String(255), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(160), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    email_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="unknown")
    linkedin_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    facebook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    x_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    company_website: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(160), nullable=True)
    service_requested: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(30), nullable=True)
    revenue_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    revenue_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    revenue_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="new")
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="SET NULL"), nullable=True
    )
    consent_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="unknown")
    consent_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead_import_batch.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=True
    )


class LeadEvent(Base):
    __tablename__ = "t_lead_event"
    __table_args__ = (Index("ix_t_lead_event_tenant_lead", "tenant_id", "lead_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LeadCustomerAction(Base):
    __tablename__ = "t_lead_customer_action"
    __table_args__ = (Index("ix_t_lead_customer_action_lead_status", "lead_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    requires_verified_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LeadImportBatch(Base):
    __tablename__ = "t_lead_import_batch"
    __table_args__ = (
        Index("ix_t_lead_import_batch_tenant_created_by", "tenant_id", "created_by"),
        UniqueConstraint("tenant_id", "created_by", "snapshot_hash", name="uq_t_lead_import_preview_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="previewed")
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LeadImportRow(Base):
    __tablename__ = "t_lead_import_row"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_t_lead_import_row_number"),
        Index("ix_t_lead_import_row_batch_status", "batch_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead_import_batch.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_tenant.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    errors: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_lead.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
