"""add Meta WhatsApp Cloud API identities and message metadata

Revision ID: c3a8f10e5d72
Revises: 9e4a7c2d1b80
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3a8f10e5d72"
down_revision: str | Sequence[str] | None = "9e4a7c2d1b80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("provider", sa.String(length=20), server_default="twilio", nullable=False),
    )
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("provider_account_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "t_whatsapp_channel",
        sa.Column("provider_channel_id", sa.String(length=128), nullable=True),
    )
    op.execute(
        "UPDATE t_whatsapp_channel SET provider_account_id = twilio_account_sid, "
        "provider_channel_id = sender_address WHERE provider = 'twilio'"
    )
    op.alter_column("t_whatsapp_channel", "twilio_account_sid", nullable=True)
    op.create_check_constraint(
        "ck_t_whatsapp_channel_provider",
        "t_whatsapp_channel",
        "provider IN ('twilio', 'meta')",
    )
    op.create_unique_constraint(
        "uq_t_whatsapp_channel_provider_identity",
        "t_whatsapp_channel",
        ["provider", "provider_account_id", "provider_channel_id"],
    )

    op.add_column(
        "t_whatsapp_message",
        sa.Column("provider", sa.String(length=20), server_default="twilio", nullable=False),
    )
    op.add_column(
        "t_whatsapp_message",
        sa.Column("message_mode", sa.String(length=30), server_default="text", nullable=False),
    )
    op.add_column(
        "t_whatsapp_message",
        sa.Column("purpose", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "t_whatsapp_message",
        sa.Column("template_snapshot", sa.JSON(), nullable=True),
    )
    op.alter_column(
        "t_whatsapp_message",
        "provider_message_sid",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.drop_constraint(
        "uq_t_whatsapp_message_provider_sid", "t_whatsapp_message", type_="unique"
    )
    op.create_unique_constraint(
        "uq_t_whatsapp_message_provider_identifier",
        "t_whatsapp_message",
        ["provider", "provider_message_sid"],
    )
    op.create_check_constraint(
        "ck_t_whatsapp_message_provider",
        "t_whatsapp_message",
        "provider IN ('twilio', 'meta')",
    )
    op.create_check_constraint(
        "ck_t_whatsapp_message_mode",
        "t_whatsapp_message",
        "message_mode IN ('text', 'approved_template')",
    )

    op.add_column(
        "t_whatsapp_delivery_event",
        sa.Column("provider", sa.String(length=20), server_default="twilio", nullable=False),
    )
    op.alter_column(
        "t_whatsapp_delivery_event",
        "provider_message_sid",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        "provider IN ('twilio', 'meta')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        type_="check",
    )
    op.alter_column(
        "t_whatsapp_delivery_event",
        "provider_message_sid",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_column("t_whatsapp_delivery_event", "provider")

    op.drop_constraint(
        "ck_t_whatsapp_message_mode", "t_whatsapp_message", type_="check"
    )
    op.drop_constraint(
        "ck_t_whatsapp_message_provider", "t_whatsapp_message", type_="check"
    )
    op.drop_constraint(
        "uq_t_whatsapp_message_provider_identifier",
        "t_whatsapp_message",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_t_whatsapp_message_provider_sid",
        "t_whatsapp_message",
        ["provider_message_sid"],
    )
    op.alter_column(
        "t_whatsapp_message",
        "provider_message_sid",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.drop_column("t_whatsapp_message", "template_snapshot")
    op.drop_column("t_whatsapp_message", "purpose")
    op.drop_column("t_whatsapp_message", "message_mode")
    op.drop_column("t_whatsapp_message", "provider")

    op.drop_constraint(
        "uq_t_whatsapp_channel_provider_identity",
        "t_whatsapp_channel",
        type_="unique",
    )
    op.drop_constraint(
        "ck_t_whatsapp_channel_provider", "t_whatsapp_channel", type_="check"
    )
    op.alter_column("t_whatsapp_channel", "twilio_account_sid", nullable=False)
    op.drop_column("t_whatsapp_channel", "provider_channel_id")
    op.drop_column("t_whatsapp_channel", "provider_account_id")
    op.drop_column("t_whatsapp_channel", "provider")
