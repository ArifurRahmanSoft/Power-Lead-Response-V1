"""add direct email skipped state

Revision ID: 3fa81d2c6e90
Revises: 0c8e7a4b2d11
Create Date: 2026-10-17 18:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = "3fa81d2c6e90"
down_revision: str | Sequence[str] | None = "0c8e7a4b2d11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_t_outbound_message_status",
        "t_outbound_message",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_outbound_message_status",
        "t_outbound_message",
        "status IN ('queued', 'processing', 'sent', 'failed', 'unknown', 'cancelled', 'skipped')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE t_outbound_message SET status = 'cancelled' "
        "WHERE status = 'skipped'"
    )
    op.drop_constraint(
        "ck_t_outbound_message_status",
        "t_outbound_message",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_outbound_message_status",
        "t_outbound_message",
        "status IN ('queued', 'processing', 'sent', 'failed', 'unknown', 'cancelled')",
    )
