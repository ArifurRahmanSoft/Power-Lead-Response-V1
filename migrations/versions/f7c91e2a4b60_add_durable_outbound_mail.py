"""add durable outbound mail

Revision ID: f7c91e2a4b60
Revises: e2a7b4c91d30
Create Date: 2026-10-15 12:00:00
"""
from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "f7c91e2a4b60"
down_revision: str | Sequence[str] | None = "e2a7b4c91d30"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "mail.send": uuid.UUID("80000000-0000-0000-0000-000000000001"),
    "mail.cancel": uuid.UUID("80000000-0000-0000-0000-000000000002"),
}
ROLES = {
    "owner": uuid.UUID("10000000-0000-0000-0000-000000000001"),
    "operator": uuid.UUID("10000000-0000-0000-0000-000000000002"),
}


def upgrade() -> None:
    op.create_table(
        "t_outbound_message_preview",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("context_type", sa.String(20), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("sender_account_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("sender_snapshot", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("asset_manifest", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("context_type IN ('lead', 'customer')", name="ck_t_outbound_preview_context"),
        sa.CheckConstraint("purpose IN ('lead_acquisition', 'customer_service')", name="ck_t_outbound_preview_purpose"),
        sa.CheckConstraint("(context_type = 'lead' AND lead_id IS NOT NULL AND customer_id IS NULL) OR (context_type = 'customer' AND customer_id IS NOT NULL AND lead_id IS NOT NULL)", name="ck_t_outbound_preview_context_refs"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["t_customer.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sender_account_id"], ["t_sender_account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_id"], ["t_mail_template.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_version_id"], ["t_mail_template_version.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_t_outbound_preview_tenant_creator", "t_outbound_message_preview", ["tenant_id", "created_by", "expires_at"])
    op.create_table(
        "t_outbound_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("context_type", sa.String(20), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("sender_account_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("sender_snapshot", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("asset_manifest", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("cancellation_reason", sa.String(500), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'processing', 'sent', 'failed', 'unknown', 'cancelled')", name="ck_t_outbound_message_status"),
        sa.CheckConstraint("context_type IN ('lead', 'customer')", name="ck_t_outbound_message_context"),
        sa.CheckConstraint("purpose IN ('lead_acquisition', 'customer_service')", name="ck_t_outbound_message_purpose"),
        sa.CheckConstraint("(context_type = 'lead' AND lead_id IS NOT NULL AND customer_id IS NULL) OR (context_type = 'customer' AND customer_id IS NOT NULL AND lead_id IS NOT NULL)", name="ck_t_outbound_message_context_refs"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preview_id"], ["t_outbound_message_preview.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["t_customer.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sender_account_id"], ["t_sender_account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_id"], ["t_mail_template.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_version_id"], ["t_mail_template_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_t_outbound_message_tenant_idempotency"),
    )
    op.create_index("ix_t_outbound_message_tenant_created", "t_outbound_message", ["tenant_id", "created_at"])
    op.create_index("ix_t_outbound_message_lead", "t_outbound_message", ["tenant_id", "lead_id", "created_at"])
    op.create_index("ix_t_outbound_message_customer", "t_outbound_message", ["tenant_id", "customer_id", "created_at"])
    op.create_table(
        "t_outbound_message_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(120), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'processing', 'retry', 'completed', 'failed', 'unknown', 'cancelled')", name="ck_t_outbound_job_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_t_outbound_job_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["t_outbound_message.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_t_outbound_message_job_message"),
    )
    op.create_index("ix_t_outbound_job_claim", "t_outbound_message_job", ["status", "available_at", "lease_expires_at"])
    op.create_table(
        "t_outbound_message_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("safe_reason", sa.String(500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["t_outbound_message.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_t_outbound_event_message_time", "t_outbound_message_event", ["message_id", "occurred_at"])
    op.add_column("t_lead_activity", sa.Column("outbound_message_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_t_lead_activity_outbound_message_id", "t_lead_activity", ["outbound_message_id"])
    op.create_foreign_key("fk_t_lead_activity_outbound_message", "t_lead_activity", "t_outbound_message", ["outbound_message_id"], ["id"], ondelete="RESTRICT")

    permission = sa.table("t_permission", sa.column("id", sa.Uuid()), sa.column("key", sa.String()), sa.column("description", sa.String()))
    role_permission = sa.table("t_role_permission", sa.column("role_id", sa.Uuid()), sa.column("permission_id", sa.Uuid()))
    op.bulk_insert(permission, [{"id": value, "key": key, "description": key.replace(".", " ").title()} for key, value in PERMISSIONS.items()])
    op.bulk_insert(role_permission, [{"role_id": role_id, "permission_id": permission_id} for role_id in ROLES.values() for permission_id in PERMISSIONS.values()])


def downgrade() -> None:
    role_permission = sa.table("t_role_permission", sa.column("permission_id", sa.Uuid()))
    permission = sa.table("t_permission", sa.column("id", sa.Uuid()))
    op.execute(role_permission.delete().where(role_permission.c.permission_id.in_(PERMISSIONS.values())))
    op.execute(permission.delete().where(permission.c.id.in_(PERMISSIONS.values())))
    op.drop_constraint("fk_t_lead_activity_outbound_message", "t_lead_activity", type_="foreignkey")
    op.drop_constraint("uq_t_lead_activity_outbound_message_id", "t_lead_activity", type_="unique")
    op.drop_column("t_lead_activity", "outbound_message_id")
    op.drop_index("ix_t_outbound_event_message_time", table_name="t_outbound_message_event")
    op.drop_table("t_outbound_message_event")
    op.drop_index("ix_t_outbound_job_claim", table_name="t_outbound_message_job")
    op.drop_table("t_outbound_message_job")
    op.drop_index("ix_t_outbound_message_customer", table_name="t_outbound_message")
    op.drop_index("ix_t_outbound_message_lead", table_name="t_outbound_message")
    op.drop_index("ix_t_outbound_message_tenant_created", table_name="t_outbound_message")
    op.drop_table("t_outbound_message")
    op.drop_index("ix_t_outbound_preview_tenant_creator", table_name="t_outbound_message_preview")
    op.drop_table("t_outbound_message_preview")
