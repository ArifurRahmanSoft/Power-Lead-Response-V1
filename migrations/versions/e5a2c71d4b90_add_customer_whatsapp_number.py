"""add optional customer WhatsApp number

Revision ID: e5a2c71d4b90
Revises: d9f4a61b2c30
Create Date: 2026-11-03 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e5a2c71d4b90"
down_revision: str | Sequence[str] | None = "d9f4a61b2c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_customer",
        sa.Column("whatsapp_number", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("t_customer", "whatsapp_number")
