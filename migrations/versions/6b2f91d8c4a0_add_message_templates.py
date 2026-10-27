"""add message templates

Revision ID: 6b2f91d8c4a0
Revises: 3fa81d2c6e90
Create Date: 2026-10-17 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b2f91d8c4a0"
down_revision: str | Sequence[str] | None = "3fa81d2c6e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "t_message_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_user_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200",
            name="ck_t_message_template_name_length",
        ),
        sa.CheckConstraint(
            "length(body) BETWEEN 1 AND 10000",
            name="ck_t_message_template_body_length",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_message_template_tenant_id",
        "t_message_template",
        ["tenant_id"],
    )
    op.create_index(
        "uq_t_message_template_active_name",
        "t_message_template",
        ["tenant_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_t_message_template_active_name",
        table_name="t_message_template",
    )
    op.drop_index(
        "ix_t_message_template_tenant_id",
        table_name="t_message_template",
    )
    op.drop_table("t_message_template")
