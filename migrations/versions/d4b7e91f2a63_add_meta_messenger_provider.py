"""allow Meta Messenger in shared conversation storage

Revision ID: d4b7e91f2a63
Revises: c3a8f10e5d72
Create Date: 2026-10-26 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "d4b7e91f2a63"
down_revision: str | Sequence[str] | None = "c3a8f10e5d72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_t_whatsapp_channel_provider", "t_whatsapp_channel", type_="check"
    )
    op.create_check_constraint(
        "ck_t_whatsapp_channel_provider",
        "t_whatsapp_channel",
        "provider IN ('twilio', 'meta', 'messenger')",
    )

    op.drop_constraint(
        "ck_t_whatsapp_message_provider", "t_whatsapp_message", type_="check"
    )
    op.create_check_constraint(
        "ck_t_whatsapp_message_provider",
        "t_whatsapp_message",
        "provider IN ('twilio', 'meta', 'messenger')",
    )

    op.drop_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        "provider IN ('twilio', 'meta', 'messenger')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_whatsapp_delivery_event_provider",
        "t_whatsapp_delivery_event",
        "provider IN ('twilio', 'meta')",
    )

    op.drop_constraint(
        "ck_t_whatsapp_message_provider", "t_whatsapp_message", type_="check"
    )
    op.create_check_constraint(
        "ck_t_whatsapp_message_provider",
        "t_whatsapp_message",
        "provider IN ('twilio', 'meta')",
    )

    op.drop_constraint(
        "ck_t_whatsapp_channel_provider", "t_whatsapp_channel", type_="check"
    )
    op.create_check_constraint(
        "ck_t_whatsapp_channel_provider",
        "t_whatsapp_channel",
        "provider IN ('twilio', 'meta')",
    )
