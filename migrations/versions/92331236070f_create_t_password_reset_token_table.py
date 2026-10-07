"""create t_password_reset_token table

Revision ID: 92331236070f
Revises: 20260928_0002
Create Date: 2026-09-29 15:01:29.908566
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "92331236070f"
down_revision: str | Sequence[str] | None = "20260928_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_password_reset_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["t_user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_password_reset_token_token_hash",
        "t_password_reset_token",
        ["token_hash"],
        unique=False,
    )
    op.create_index(
        "ix_t_password_reset_token_user_id",
        "t_password_reset_token",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_password_reset_token_user_id",
        table_name="t_password_reset_token",
    )
    op.drop_index(
        "ix_t_password_reset_token_token_hash",
        table_name="t_password_reset_token",
    )
    op.drop_table("t_password_reset_token")

