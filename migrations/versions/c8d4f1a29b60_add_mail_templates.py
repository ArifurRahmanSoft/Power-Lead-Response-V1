"""add mail templates

Revision ID: c8d4f1a29b60
Revises: b72a901df304
Create Date: 2026-10-12 12:00:00
"""
from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "c8d4f1a29b60"
down_revision: str | Sequence[str] | None = "b72a901df304"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_ROLE_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
PERMISSION_IDS = {
    "mail.templates.view": uuid.UUID("60000000-0000-0000-0000-000000000001"),
    "mail.templates.manage": uuid.UUID("60000000-0000-0000-0000-000000000002"),
    "mail.templates.approve": uuid.UUID("60000000-0000-0000-0000-000000000003"),
}


def upgrade() -> None:
    op.create_table(
        "t_mail_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["archived_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_t_mail_template_tenant_id", "t_mail_template", ["tenant_id"], unique=False)
    op.create_index(
        "uq_t_mail_template_active_name",
        "t_mail_template",
        ["tenant_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
        sqlite_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "t_mail_template_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("signature_html", sa.Text(), server_default="", nullable=False),
        sa.Column("signature_text", sa.Text(), server_default="", nullable=False),
        sa.Column("approval_status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "approval_status IN ('draft', 'approved')",
            name="ck_t_mail_template_version_approval_status",
        ),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_id"], ["t_mail_template.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_t_mail_template_version_number"),
    )
    op.create_index(
        "ix_t_mail_template_version_tenant_template",
        "t_mail_template_version",
        ["tenant_id", "template_id"],
        unique=False,
    )
    op.create_index(
        "uq_t_mail_template_single_draft",
        "t_mail_template_version",
        ["template_id"],
        unique=True,
        postgresql_where=sa.text("approval_status = 'draft'"),
        sqlite_where=sa.text("approval_status = 'draft'"),
    )

    op.create_table(
        "t_mail_template_asset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('inline_body', 'signature_image', 'attachment')",
            name="ck_t_mail_template_asset_kind",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_t_mail_template_asset_tenant_id", "t_mail_template_asset", ["tenant_id"], unique=False)
    op.create_index("ix_t_mail_template_asset_sha256", "t_mail_template_asset", ["tenant_id", "sha256"], unique=False)

    op.create_table(
        "t_mail_template_version_asset",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["t_mail_template_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id"], ["t_mail_template_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("version_id", "asset_id"),
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
    role_permission_table = sa.table("t_role_permission", sa.column("permission_id", sa.Uuid()))
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
    op.drop_table("t_mail_template_version_asset")
    op.drop_index("ix_t_mail_template_asset_sha256", table_name="t_mail_template_asset")
    op.drop_index("ix_t_mail_template_asset_tenant_id", table_name="t_mail_template_asset")
    op.drop_table("t_mail_template_asset")
    op.drop_index("uq_t_mail_template_single_draft", table_name="t_mail_template_version")
    op.drop_index("ix_t_mail_template_version_tenant_template", table_name="t_mail_template_version")
    op.drop_table("t_mail_template_version")
    op.drop_index("uq_t_mail_template_active_name", table_name="t_mail_template")
    op.drop_index("ix_t_mail_template_tenant_id", table_name="t_mail_template")
    op.drop_table("t_mail_template")
