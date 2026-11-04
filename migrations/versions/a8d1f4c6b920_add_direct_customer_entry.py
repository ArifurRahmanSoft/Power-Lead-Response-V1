"""add direct customer entry

Revision ID: a8d1f4c6b920
Revises: f6b4d2a9c801
Create Date: 2026-11-03
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "a8d1f4c6b920"
down_revision: str | None = "f6b4d2a9c801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_IDS = {
    "owner": uuid.UUID("10000000-0000-0000-0000-000000000001"),
    "operator": uuid.UUID("10000000-0000-0000-0000-000000000002"),
}
PERMISSION_IDS = {
    "customers.create": uuid.UUID("70000000-0000-0000-0000-000000000003"),
    "customers.update": uuid.UUID("70000000-0000-0000-0000-000000000004"),
}


def upgrade() -> None:
    columns = (
        sa.Column("designation", sa.String(length=160), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("linkedin_url", sa.String(length=2048), nullable=True),
        sa.Column("facebook_url", sa.String(length=2048), nullable=True),
        sa.Column("instagram_url", sa.String(length=2048), nullable=True),
        sa.Column("x_url", sa.String(length=2048), nullable=True),
        sa.Column("github_url", sa.String(length=2048), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("company_website", sa.String(length=2048), nullable=True),
        sa.Column("industry", sa.String(length=160), nullable=True),
        sa.Column("service_requested", sa.String(length=200), nullable=True),
        sa.Column("company_size", sa.String(length=30), nullable=True),
        sa.Column("revenue_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("revenue_currency", sa.String(length=3), nullable=True),
        sa.Column("revenue_period", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(length=20), nullable=True),
        sa.Column("consent_evidence", sa.Text(), nullable=True),
        sa.Column("consent_reference", sa.String(length=255), nullable=True),
    )
    for column in columns:
        op.add_column("t_customer", column)

    op.create_foreign_key(
        "fk_t_customer_assigned_user",
        "t_customer",
        "t_user",
        ["assigned_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_t_customer_assigned_user_id",
        "t_customer",
        ["assigned_user_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_t_customer_source_type",
        "t_customer",
        "source_type IS NULL OR source_type IN ('direct', 'lead')",
    )
    op.create_check_constraint(
        "ck_t_customer_revenue_nonnegative",
        "t_customer",
        "revenue_amount IS NULL OR revenue_amount >= 0",
    )
    op.create_check_constraint(
        "ck_t_customer_revenue_complete",
        "t_customer",
        "(revenue_amount IS NULL AND revenue_currency IS NULL AND revenue_period IS NULL) OR "
        "(revenue_amount IS NOT NULL AND revenue_currency IS NOT NULL AND revenue_period IS NOT NULL)",
    )
    op.execute(
        sa.text(
            "UPDATE t_customer AS customer SET source_type = 'lead' "
            "WHERE source_type IS NULL AND EXISTS ("
            "SELECT 1 FROM t_lead_conversion AS conversion "
            "WHERE conversion.customer_id = customer.id "
            "AND conversion.tenant_id = customer.tenant_id)"
        )
    )

    permission_table = sa.table(
        "t_permission",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("description", sa.String()),
    )
    role_permission_table = sa.table(
        "t_role_permission",
        sa.column("role_id", sa.Uuid()),
        sa.column("permission_id", sa.Uuid()),
    )
    op.bulk_insert(
        permission_table,
        [
            {
                "id": permission_id,
                "key": key,
                "description": key.replace(".", " ").title(),
            }
            for key, permission_id in PERMISSION_IDS.items()
        ],
    )
    op.bulk_insert(
        role_permission_table,
        [
            {
                "role_id": role_id,
                "permission_id": permission_id,
            }
            for role_id in ROLE_IDS.values()
            for permission_id in PERMISSION_IDS.values()
        ],
    )


def downgrade() -> None:
    role_permission_table = sa.table(
        "t_role_permission",
        sa.column("permission_id", sa.Uuid()),
    )
    permission_table = sa.table(
        "t_permission",
        sa.column("id", sa.Uuid()),
    )
    op.execute(
        role_permission_table.delete().where(
            role_permission_table.c.permission_id.in_(PERMISSION_IDS.values())
        )
    )
    op.execute(
        permission_table.delete().where(
            permission_table.c.id.in_(PERMISSION_IDS.values())
        )
    )

    op.drop_constraint(
        "ck_t_customer_revenue_complete",
        "t_customer",
        type_="check",
    )
    op.drop_constraint(
        "ck_t_customer_revenue_nonnegative",
        "t_customer",
        type_="check",
    )
    op.drop_constraint("ck_t_customer_source_type", "t_customer", type_="check")
    op.drop_index("ix_t_customer_assigned_user_id", table_name="t_customer")
    op.drop_constraint(
        "fk_t_customer_assigned_user",
        "t_customer",
        type_="foreignkey",
    )
    for name in (
        "consent_reference",
        "consent_evidence",
        "source_type",
        "assigned_user_id",
        "notes",
        "revenue_period",
        "revenue_currency",
        "revenue_amount",
        "company_size",
        "service_requested",
        "industry",
        "company_website",
        "state",
        "city",
        "github_url",
        "x_url",
        "instagram_url",
        "facebook_url",
        "linkedin_url",
        "country",
        "designation",
    ):
        op.drop_column("t_customer", name)
