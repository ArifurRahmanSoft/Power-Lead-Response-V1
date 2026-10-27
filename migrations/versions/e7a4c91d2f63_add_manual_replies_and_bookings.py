"""add manual replies and bookings

Revision ID: e7a4c91d2f63
Revises: f3b8d92a7c41
Create Date: 2026-10-13 17:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e7a4c91d2f63"
down_revision: str | Sequence[str] | None = "f3b8d92a7c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_lead_activity",
        sa.Column(
            "provider_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_table(
        "t_lead_booking",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("responsible_user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="scheduled", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "provider_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "source IN ('manual', 'email', 'phone', 'sms', 'whatsapp', 'other')",
            name="ck_t_lead_booking_source",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'confirmed', 'cancelled', 'completed')",
            name="ck_t_lead_booking_status",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_t_lead_booking_row_version_positive"),
        sa.ForeignKeyConstraint(["created_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["responsible_user_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_booking_tenant_lead_scheduled",
        "t_lead_booking",
        ["tenant_id", "lead_id", "scheduled_at"],
        unique=False,
    )
    op.create_index(
        "ix_t_lead_booking_responsible_status",
        "t_lead_booking",
        ["responsible_user_id", "status"],
        unique=False,
    )
    op.create_table(
        "t_lead_booking_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("previous_status", sa.String(length=20), nullable=True),
        sa.Column("new_status", sa.String(length=20), nullable=False),
        sa.Column("previous_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_timezone", sa.String(length=100), nullable=True),
        sa.Column("new_timezone", sa.String(length=100), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'confirmed', 'cancelled', 'rescheduled')",
            name="ck_t_lead_booking_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["t_lead_booking.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recorded_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_booking_event_tenant_booking",
        "t_lead_booking_event",
        ["tenant_id", "booking_id", "recorded_at"],
        unique=False,
    )
    op.create_table(
        "t_lead_automation_stop_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stop_key",
            sa.String(length=80),
            server_default="recovery_follow_up",
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(length=40), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trigger_type IN ('manual_reply', 'booking_created', 'booking_confirmed', 'booking_rescheduled')",
            name="ck_t_lead_automation_stop_trigger_type",
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recorded_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_automation_stop_tenant_lead_key",
        "t_lead_automation_stop_event",
        ["tenant_id", "lead_id", "stop_key", "recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_lead_automation_stop_tenant_lead_key",
        table_name="t_lead_automation_stop_event",
    )
    op.drop_table("t_lead_automation_stop_event")
    op.drop_index(
        "ix_t_lead_booking_event_tenant_booking", table_name="t_lead_booking_event"
    )
    op.drop_table("t_lead_booking_event")
    op.drop_index("ix_t_lead_booking_responsible_status", table_name="t_lead_booking")
    op.drop_index("ix_t_lead_booking_tenant_lead_scheduled", table_name="t_lead_booking")
    op.drop_table("t_lead_booking")
    op.drop_column("t_lead_activity", "provider_verified")
