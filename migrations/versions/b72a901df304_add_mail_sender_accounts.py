"""add mail sender accounts

Revision ID: b72a901df304
Revises: e1c28a9ad51e
Create Date: 2026-10-11 14:00:00
"""
from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "b72a901df304"
down_revision: str | Sequence[str] | None = "e1c28a9ad51e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_ROLE_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
PERMISSION_IDS = {
    "mail.view": uuid.UUID("50000000-0000-0000-0000-000000000001"),
    "mail.manage": uuid.UUID("50000000-0000-0000-0000-000000000002"),
    "mail.test": uuid.UUID("50000000-0000-0000-0000-000000000003"),
}


def upgrade() -> None:
    op.create_table(
        "t_sender_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("sender_email", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("authentication_type", sa.String(length=30), nullable=False),
        sa.Column("smtp_host", sa.String(length=255), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("encryption", sa.String(length=20), nullable=False),
        sa.Column("smtp_username", sa.String(length=255), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=False),
        sa.Column("reply_to", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "connection_status",
            sa.String(length=20),
            server_default="not_tested",
            nullable=False,
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_message", sa.String(length=160), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "provider IN ('gmail', 'custom_smtp')",
            name="ck_t_sender_account_provider",
        ),
        sa.CheckConstraint(
            "authentication_type IN ('app_password', 'password')",
            name="ck_t_sender_account_authentication_type",
        ),
        sa.CheckConstraint(
            "encryption IN ('STARTTLS', 'TLS')",
            name="ck_t_sender_account_encryption",
        ),
        sa.CheckConstraint(
            "connection_status IN ('not_tested', 'success', 'failed')",
            name="ck_t_sender_account_connection_status",
        ),
        sa.CheckConstraint(
            "smtp_port BETWEEN 1 AND 65535",
            name="ck_t_sender_account_smtp_port",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["t_user.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["t_tenant.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["t_user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "sender_email",
            name="uq_t_sender_account_tenant_email",
        ),
    )
    op.create_index(
        "ix_t_sender_account_tenant_active",
        "t_sender_account",
        ["tenant_id", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_t_sender_account_tenant_id",
        "t_sender_account",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "uq_t_sender_account_default_tenant",
        "t_sender_account",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
        sqlite_where=sa.text("is_default = 1"),
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
            {"role_id": OWNER_ROLE_ID, "permission_id": permission_id}
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
    op.drop_index("uq_t_sender_account_default_tenant", table_name="t_sender_account")
    op.drop_index("ix_t_sender_account_tenant_id", table_name="t_sender_account")
    op.drop_index("ix_t_sender_account_tenant_active", table_name="t_sender_account")
    op.drop_table("t_sender_account")
