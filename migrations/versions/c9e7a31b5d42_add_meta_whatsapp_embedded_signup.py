"""add Meta WhatsApp Embedded Signup operations

Revision ID: c9e7a31b5d42
Revises: a8d1f4c6b920
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c9e7a31b5d42"
down_revision: str | None = "a8d1f4c6b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("connection_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("subscription_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("subscription_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "t_meta_whatsapp_oauth_operation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=True),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration_id", sa.String(length=128), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("provider_http_status", sa.Integer(), nullable=True),
        sa.Column("discovered_business_id", sa.String(length=128), nullable=True),
        sa.Column("discovered_waba_id", sa.String(length=128), nullable=True),
        sa.Column("discovered_phone_number_id", sa.String(length=128), nullable=True),
        sa.Column("discovered_sender_address", sa.String(length=32), nullable=True),
        sa.Column("discovered_display_name", sa.String(length=200), nullable=True),
        sa.Column("connection_mode", sa.String(length=32), nullable=True),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column("subscription_succeeded", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["channel_id"], ["t_whatsapp_channel.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "state_hash", name="uq_t_meta_whatsapp_oauth_operation_state_hash"
        ),
    )
    op.create_index(
        "ix_t_meta_whatsapp_oauth_operation_expiry",
        "t_meta_whatsapp_oauth_operation",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_t_meta_whatsapp_oauth_operation_tenant",
        "t_meta_whatsapp_oauth_operation",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_t_meta_whatsapp_oauth_operation_tenant",
        table_name="t_meta_whatsapp_oauth_operation",
    )
    op.drop_index(
        "ix_t_meta_whatsapp_oauth_operation_expiry",
        table_name="t_meta_whatsapp_oauth_operation",
    )
    op.drop_table("t_meta_whatsapp_oauth_operation")
    op.drop_column("t_whatsapp_channel", "subscription_checked_at")
    op.drop_column("t_whatsapp_channel", "subscription_status")
    op.drop_column("t_whatsapp_channel", "connection_mode")
