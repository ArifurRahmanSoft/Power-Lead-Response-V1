import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Customer(Base):
    __tablename__ = "t_customer"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "email",
            name="uq_t_customer_tenant_email",
        ),
        Index("ix_t_customer_tenant_name", "tenant_id", "last_name", "first_name"),
        CheckConstraint(
            "communication_purpose = 'customer_relationship'",
            name="ck_t_customer_communication_purpose",
        ),
        CheckConstraint(
            "communication_consent_status IN "
            "('unknown', 'not_requested', 'granted', 'denied', 'withdrawn')",
            name="ck_t_customer_communication_consent",
        ),
        CheckConstraint("row_version >= 1", name="ck_t_customer_row_version_positive"),
        CheckConstraint(
            "source_type IS NULL OR source_type IN ('direct', 'lead')",
            name="ck_t_customer_source_type",
        ),
        CheckConstraint(
            "revenue_amount IS NULL OR revenue_amount >= 0",
            name="ck_t_customer_revenue_nonnegative",
        ),
        CheckConstraint(
            "(revenue_amount IS NULL AND revenue_currency IS NULL AND revenue_period IS NULL) OR "
            "(revenue_amount IS NOT NULL AND revenue_currency IS NOT NULL AND revenue_period IS NOT NULL)",
            name="ck_t_customer_revenue_complete",
        ),
        Index("ix_t_customer_assigned_user_id", "assigned_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_tenant.id", ondelete="CASCADE"),
        nullable=False,
    )
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(160), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    facebook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    x_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    communication_purpose: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="customer_relationship",
        server_default="customer_relationship",
    )
    communication_consent_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown", server_default="unknown"
    )
    consent_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_restrictions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    suppressed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suppression_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class LeadConversion(Base):
    __tablename__ = "t_lead_conversion"
    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_t_lead_conversion_lead"),
        Index(
            "ix_t_lead_conversion_tenant_customer",
            "tenant_id",
            "customer_id",
            "converted_at",
        ),
        CheckConstraint(
            "duplicate_resolution IN ('create_new', 'link_existing')",
            name="ck_t_lead_conversion_duplicate_resolution",
        ),
        CheckConstraint(
            "pending_task_disposition IN ('cancel', 'retain')",
            name="ck_t_lead_conversion_task_disposition",
        ),
        CheckConstraint(
            "cancelled_nurture_job_count >= 0 AND affected_task_count >= 0",
            name="ck_t_lead_conversion_counts_nonnegative",
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
        ForeignKey("t_lead.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_customer.id", ondelete="RESTRICT"),
        nullable=False,
    )
    converted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    converted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("t_user.id", ondelete="RESTRICT"), nullable=False
    )
    purchased_service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duplicate_resolution: Mapped[str] = mapped_column(String(30), nullable=False)
    pending_task_disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    pending_task_reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    cancelled_nurture_job_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    affected_task_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_consent_status: Mapped[str] = mapped_column(String(30), nullable=False)
    source_consent_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_consent_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
