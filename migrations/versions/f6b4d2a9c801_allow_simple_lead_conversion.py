"""allow simple lead conversion

Revision ID: f6b4d2a9c801
Revises: e5a2c71d4b90
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f6b4d2a9c801"
down_revision: str | None = "e5a2c71d4b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "t_lead_conversion",
        "purchased_service",
        existing_type=sa.String(length=200),
        nullable=True,
    )
    op.alter_column(
        "t_lead_conversion",
        "reason",
        existing_type=sa.String(length=1000),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE t_lead_conversion SET purchased_service = '' "
            "WHERE purchased_service IS NULL"
        )
    )
    op.execute(
        sa.text("UPDATE t_lead_conversion SET reason = '' WHERE reason IS NULL")
    )
    op.alter_column(
        "t_lead_conversion",
        "reason",
        existing_type=sa.String(length=1000),
        nullable=False,
    )
    op.alter_column(
        "t_lead_conversion",
        "purchased_service",
        existing_type=sa.String(length=200),
        nullable=False,
    )
