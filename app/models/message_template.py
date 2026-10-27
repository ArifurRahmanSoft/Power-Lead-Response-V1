import uuid
from datetime import datetime

from sqlalchemy import (
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


class MessageTemplate(Base):
    __tablename__ = "t_message_template"
    __table_args__ = (
        CheckConstraint(
            "length(name) BETWEEN 1 AND 200",
            name="ck_t_message_template_name_length",
        ),
        CheckConstraint(
            "length(body) BETWEEN 1 AND 10000",
            name="ck_t_message_template_body_length",
        ),
        Index("ix_t_message_template_tenant_id", "tenant_id"),
        Index(
            "uq_t_message_template_active_name",
            "tenant_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
            sqlite_where=text("archived_at IS NULL"),
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
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
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
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("t_user.id", ondelete="RESTRICT"),
        nullable=True,
    )
