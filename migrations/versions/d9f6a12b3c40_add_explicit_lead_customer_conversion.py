"""add explicit lead customer conversion

Revision ID: d9f6a12b3c40
Revises: b5e19f30a842
Create Date: 2026-10-14 12:00:00
"""
from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "d9f6a12b3c40"
down_revision: str | Sequence[str] | None = "b5e19f30a842"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_IDS = {
    "owner": uuid.UUID("10000000-0000-0000-0000-000000000001"),
    "operator": uuid.UUID("10000000-0000-0000-0000-000000000002"),
    "staff": uuid.UUID("10000000-0000-0000-0000-000000000003"),
    "readonly": uuid.UUID("10000000-0000-0000-0000-000000000004"),
}
PERMISSION_IDS = {
    "customers.view": uuid.UUID("70000000-0000-0000-0000-000000000001"),
    "customers.convert": uuid.UUID("70000000-0000-0000-0000-000000000002"),
}
ROLE_PERMISSION_KEYS = {
    "owner": {"customers.view", "customers.convert"},
    "operator": {"customers.view", "customers.convert"},
    "staff": {"customers.view"},
    "readonly": {"customers.view"},
}


def upgrade() -> None:
    op.create_table(
        "t_customer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("first_name", sa.String(length=120), nullable=False),
        sa.Column("last_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=80), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column(
            "communication_purpose",
            sa.String(length=40),
            server_default="customer_relationship",
            nullable=False,
        ),
        sa.Column(
            "communication_consent_status",
            sa.String(length=30),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "channel_restrictions",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppression_reason", sa.String(length=1000), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "communication_purpose = 'customer_relationship'",
            name="ck_t_customer_communication_purpose",
        ),
        sa.CheckConstraint(
            "communication_consent_status IN "
            "('unknown', 'not_requested', 'granted', 'denied', 'withdrawn')",
            name="ck_t_customer_communication_consent",
        ),
        sa.CheckConstraint(
            "row_version >= 1",
            name="ck_t_customer_row_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "email", name="uq_t_customer_tenant_email"
        ),
    )
    op.create_index(
        "ix_t_customer_tenant_name",
        "t_customer",
        ["tenant_id", "last_name", "first_name"],
        unique=False,
    )

    op.create_table(
        "t_lead_conversion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "converted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("converted_by", sa.Uuid(), nullable=False),
        sa.Column("purchased_service", sa.String(length=200), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("duplicate_resolution", sa.String(length=30), nullable=False),
        sa.Column("pending_task_disposition", sa.String(length=20), nullable=False),
        sa.Column("pending_task_reason", sa.String(length=1000), nullable=False),
        sa.Column(
            "cancelled_nurture_job_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "affected_task_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("source_consent_status", sa.String(length=30), nullable=False),
        sa.Column("source_consent_evidence", sa.Text(), nullable=True),
        sa.Column("source_consent_reference", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "duplicate_resolution IN ('create_new', 'link_existing')",
            name="ck_t_lead_conversion_duplicate_resolution",
        ),
        sa.CheckConstraint(
            "pending_task_disposition IN ('cancel', 'retain')",
            name="ck_t_lead_conversion_task_disposition",
        ),
        sa.CheckConstraint(
            "cancelled_nurture_job_count >= 0 AND affected_task_count >= 0",
            name="ck_t_lead_conversion_counts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["converted_by"], ["t_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["t_customer.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", name="uq_t_lead_conversion_lead"),
    )
    op.create_index(
        "ix_t_lead_conversion_tenant_customer",
        "t_lead_conversion",
        ["tenant_id", "customer_id", "converted_at"],
        unique=False,
    )

    op.add_column(
        "t_lead_task",
        sa.Column("cancellation_reason", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "t_lead_customer_action",
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "t_lead_customer_action",
        sa.Column("cancellation_reason", sa.String(length=1000), nullable=True),
    )
    op.create_foreign_key(
        "fk_t_lead_customer_action_cancelled_by_user",
        "t_lead_customer_action",
        "t_user",
        ["cancelled_by"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "ck_t_lead_automation_stop_trigger_type",
        "t_lead_automation_stop_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_lead_automation_stop_trigger_type",
        "t_lead_automation_stop_event",
        "trigger_type IN ('manual_reply', 'booking_created', 'booking_confirmed', "
        "'booking_rescheduled', 'lead_converted')",
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
                "role_id": ROLE_IDS[role_key],
                "permission_id": PERMISSION_IDS[permission_key],
            }
            for role_key, permission_keys in ROLE_PERMISSION_KEYS.items()
            for permission_key in sorted(permission_keys)
        ],
    )


def downgrade() -> None:
    role_permission_table = sa.table(
        "t_role_permission", sa.column("permission_id", sa.Uuid())
    )
    permission_table = sa.table("t_permission", sa.column("id", sa.Uuid()))
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
        "ck_t_lead_automation_stop_trigger_type",
        "t_lead_automation_stop_event",
        type_="check",
    )
    op.create_check_constraint(
        "ck_t_lead_automation_stop_trigger_type",
        "t_lead_automation_stop_event",
        "trigger_type IN ('manual_reply', 'booking_created', 'booking_confirmed', "
        "'booking_rescheduled')",
    )
    op.drop_constraint(
        "fk_t_lead_customer_action_cancelled_by_user",
        "t_lead_customer_action",
        type_="foreignkey",
    )
    op.drop_column("t_lead_customer_action", "cancellation_reason")
    op.drop_column("t_lead_customer_action", "cancelled_by")
    op.drop_column("t_lead_task", "cancellation_reason")
    op.drop_index(
        "ix_t_lead_conversion_tenant_customer",
        table_name="t_lead_conversion",
    )
    op.drop_table("t_lead_conversion")
    op.drop_index("ix_t_customer_tenant_name", table_name="t_customer")
    op.drop_table("t_customer")
