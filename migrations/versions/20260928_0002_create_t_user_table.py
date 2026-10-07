"""create t_user table

Revision ID: 20260928_0002
Revises: 20260928_0001
Create Date: 2026-09-28
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260928_0002"
down_revision: str | Sequence[str] | None = "20260928_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("users", "t_user")
    op.execute(
        "ALTER TABLE t_user RENAME CONSTRAINT users_pkey TO t_user_pkey"
    )

    op.add_column(
        "t_user",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    op.drop_index("ix_users_email", table_name="t_user")
    op.create_unique_constraint("uq_t_user_email", "t_user", ["email"])
    op.create_index("ix_t_user_email", "t_user", ["email"], unique=False)

    op.execute(
        """
        CREATE FUNCTION set_t_user_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_t_user_updated_at
        BEFORE UPDATE ON t_user
        FOR EACH ROW
        EXECUTE FUNCTION set_t_user_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_t_user_updated_at ON t_user")
    op.execute("DROP FUNCTION IF EXISTS set_t_user_updated_at()")

    op.drop_index("ix_t_user_email", table_name="t_user")
    op.drop_constraint("uq_t_user_email", "t_user", type_="unique")
    op.create_index("ix_users_email", "t_user", ["email"], unique=True)

    op.drop_column("t_user", "is_active")
    op.execute(
        "ALTER TABLE t_user RENAME CONSTRAINT t_user_pkey TO users_pkey"
    )
    op.rename_table("t_user", "users")
