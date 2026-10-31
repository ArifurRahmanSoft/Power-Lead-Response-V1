"""add secure Meta Instagram Unified Login OAuth connection storage

Revision ID: b7e2c41d8f90
Revises: a6d1e93c5f20
Create Date: 2026-10-31 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b7e2c41d8f90"
down_revision: str | Sequence[str] | None = "a6d1e93c5f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
    )
    op.create_table(
        "t_meta_instagram_oauth_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["t_whatsapp_channel.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_t_meta_instagram_oauth_state_hash"),
    )
    op.create_index(
        "ix_t_meta_instagram_oauth_state_expiry",
        "t_meta_instagram_oauth_state",
        ["expires_at"],
    )
    op.create_index(
        "ix_t_meta_instagram_oauth_state_tenant",
        "t_meta_instagram_oauth_state",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_meta_instagram_oauth_state_tenant",
        table_name="t_meta_instagram_oauth_state",
    )
    op.drop_index(
        "ix_t_meta_instagram_oauth_state_expiry",
        table_name="t_meta_instagram_oauth_state",
    )
    op.drop_table("t_meta_instagram_oauth_state")
    op.drop_column("t_whatsapp_channel", "credential_ciphertext")
