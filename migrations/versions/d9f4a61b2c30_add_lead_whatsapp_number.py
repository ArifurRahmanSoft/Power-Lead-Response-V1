"""add optional lead WhatsApp number

Revision ID: d9f4a61b2c30
Revises: c8f3d52e9a01
Create Date: 2026-11-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d9f4a61b2c30"
down_revision: str | Sequence[str] | None = "c8f3d52e9a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_lead",
        sa.Column("whatsapp_number", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("t_lead", "whatsapp_number")
