"""add lead priority and assignment history

Revision ID: b5e19f30a842
Revises: e7a4c91d2f63
Create Date: 2026-10-13 19:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b5e19f30a842"
down_revision: str | Sequence[str] | None = "e7a4c91d2f63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_lead",
        sa.Column("priority", sa.String(length=20), server_default="normal", nullable=False),
    )
    op.create_check_constraint(
        "ck_t_lead_priority",
        "t_lead",
        "priority IN ('low', 'normal', 'high', 'urgent')",
    )

    op.drop_constraint("ck_t_lead_task_priority", "t_lead_task", type_="check")
    op.execute("UPDATE t_lead_task SET priority = 'normal' WHERE priority = 'medium'")
    op.alter_column(
        "t_lead_task",
        "priority",
        existing_type=sa.String(length=20),
        server_default="normal",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_t_lead_task_priority",
        "t_lead_task",
        "priority IN ('low', 'normal', 'high', 'urgent')",
    )

    op.create_table(
        "t_lead_assignment_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("previous_assignee_id", sa.Uuid(), nullable=True),
        sa.Column("new_assignee_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("task_disposition", sa.String(length=20), nullable=False),
        sa.Column("affected_task_count", sa.Integer(), nullable=False),
        sa.Column("lead_row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "task_disposition IN ('transfer', 'retain', 'cancel')",
            name="ck_t_lead_assignment_task_disposition",
        ),
        sa.CheckConstraint(
            "affected_task_count >= 0",
            name="ck_t_lead_assignment_affected_task_count",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["new_assignee_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_assignee_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_lead_assignment_history_tenant_lead",
        "t_lead_assignment_history",
        ["tenant_id", "lead_id", "assigned_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_lead_assignment_history_tenant_lead",
        table_name="t_lead_assignment_history",
    )
    op.drop_table("t_lead_assignment_history")

    op.drop_constraint("ck_t_lead_task_priority", "t_lead_task", type_="check")
    op.execute("UPDATE t_lead_task SET priority = 'medium' WHERE priority = 'normal'")
    op.alter_column(
        "t_lead_task",
        "priority",
        existing_type=sa.String(length=20),
        server_default="medium",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_t_lead_task_priority",
        "t_lead_task",
        "priority IN ('low', 'medium', 'high', 'urgent')",
    )
    op.drop_constraint("ck_t_lead_priority", "t_lead", type_="check")
    op.drop_column("t_lead", "priority")
