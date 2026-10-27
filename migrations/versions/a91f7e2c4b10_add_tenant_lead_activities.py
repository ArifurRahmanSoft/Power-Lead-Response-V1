"""add tenant lead activities

Revision ID: a91f7e2c4b10
Revises: c8d4f1a29b60
Create Date: 2026-10-13 12:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a91f7e2c4b10"
down_revision: str | Sequence[str] | None = "c8d4f1a29b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_lead_activity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("activity_type", sa.String(length=30), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("performed_by", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("related_activity_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_amendment",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "externally_performed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("amendment_reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "activity_type IN ('initial_contact', 'follow_up', 'inbound_reply', 'note')",
            name="ck_t_lead_activity_type",
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'phone', 'sms', 'whatsapp', 'other')",
            name="ck_t_lead_activity_channel",
        ),
        sa.CheckConstraint(
            "direction IN ('outbound', 'inbound', 'internal')",
            name="ck_t_lead_activity_direction",
        ),
        sa.CheckConstraint(
            "related_activity_id IS NULL OR related_activity_id <> id",
            name="ck_t_lead_activity_not_self_related",
        ),
        sa.CheckConstraint(
            "NOT is_system OR NOT is_amendment",
            name="ck_t_lead_activity_system_not_amendment",
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["performed_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["recorded_by"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["related_activity_id"],
            ["t_lead_activity.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_activity_tenant_lead_timeline",
        "t_lead_activity",
        ["tenant_id", "lead_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_t_lead_activity_performed_by",
        "t_lead_activity",
        ["performed_by"],
        unique=False,
    )
    op.create_index(
        "ix_t_lead_activity_related_activity_id",
        "t_lead_activity",
        ["related_activity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_lead_activity_related_activity_id", table_name="t_lead_activity"
    )
    op.drop_index("ix_t_lead_activity_performed_by", table_name="t_lead_activity")
    op.drop_index(
        "ix_t_lead_activity_tenant_lead_timeline", table_name="t_lead_activity"
    )
    op.drop_table("t_lead_activity")
