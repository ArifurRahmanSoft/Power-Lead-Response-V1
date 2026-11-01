"""retain sanitized provider HTTP status for messaging failures

Revision ID: c8f3d52e9a01
Revises: b7e2c41d8f90
Create Date: 2026-11-01 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c8f3d52e9a01"
down_revision: str | Sequence[str] | None = "b7e2c41d8f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_whatsapp_message",
        sa.Column("provider_http_status", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("t_whatsapp_message", "provider_http_status")
