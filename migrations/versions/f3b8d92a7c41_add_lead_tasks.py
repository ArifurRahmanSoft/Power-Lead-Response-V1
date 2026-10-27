"""add lead tasks

Revision ID: f3b8d92a7c41
Revises: a91f7e2c4b10
Create Date: 2026-10-13 15:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f3b8d92a7c41"
down_revision: str | Sequence[str] | None = "a91f7e2c4b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_lead_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.String(length=20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("related_activity_id", sa.Uuid(), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "task_type IN ('contact', 'follow_up', 'review', 'other')",
            name="ck_t_lead_task_type",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_t_lead_task_priority",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')",
            name="ck_t_lead_task_status",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_t_lead_task_row_version_positive"),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_by IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_by IS NULL AND completed_at IS NULL)",
            name="ck_t_lead_task_completion_metadata",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_by IS NULL AND cancelled_at IS NULL)",
            name="ck_t_lead_task_cancellation_metadata",
        ),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["completed_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["related_activity_id"], ["t_lead_activity.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_task_tenant_lead",
        "t_lead_task",
        ["tenant_id", "lead_id"],
        unique=False,
    )
    op.create_index(
        "ix_t_lead_task_tenant_assignee_status_due",
        "t_lead_task",
        ["tenant_id", "assigned_user_id", "status", "due_at"],
        unique=False,
    )
    op.create_index(
        "ix_t_lead_task_related_activity_id",
        "t_lead_task",
        ["related_activity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_t_lead_task_related_activity_id", table_name="t_lead_task")
    op.drop_index(
        "ix_t_lead_task_tenant_assignee_status_due", table_name="t_lead_task"
    )
    op.drop_index("ix_t_lead_task_tenant_lead", table_name="t_lead_task")
    op.drop_table("t_lead_task")
