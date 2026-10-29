"""allow Meta Instagram Unified Login in shared messaging storage

Revision ID: a6d1e93c5f20
Revises: f1c8a42d6e90
Create Date: 2026-10-29 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a6d1e93c5f20"
down_revision: str | Sequence[str] | None = "f1c8a42d6e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROVIDER_CHECKS = (
    ("t_whatsapp_channel", "ck_t_whatsapp_channel_provider"),
    ("t_whatsapp_message", "ck_t_whatsapp_message_provider"),
    ("t_whatsapp_delivery_event", "ck_t_whatsapp_delivery_event_provider"),
)


def _replace(expression: str) -> None:
    for table_name, constraint_name in PROVIDER_CHECKS:
        op.drop_constraint(constraint_name, table_name, type_="check")
        op.create_check_constraint(constraint_name, table_name, expression)


def upgrade() -> None:
    _replace("provider IN ('twilio', 'meta', 'messenger', 'instagram')")


def downgrade() -> None:
    _replace("provider IN ('twilio', 'meta', 'messenger')")
