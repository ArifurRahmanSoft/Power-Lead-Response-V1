"""allow lead unassignment history

Revision ID: e2a7b4c91d30
Revises: d9f6a12b3c40
Create Date: 2026-10-14 14:30:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e2a7b4c91d30"
down_revision: str | Sequence[str] | None = "d9f6a12b3c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "t_lead_assignment_history",
        "new_assignee_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM t_lead_assignment_history WHERE new_assignee_id IS NULL"
    )
    op.alter_column(
        "t_lead_assignment_history",
        "new_assignee_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
