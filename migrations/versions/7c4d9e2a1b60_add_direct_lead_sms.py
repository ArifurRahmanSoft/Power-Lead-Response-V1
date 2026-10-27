"""add direct lead sms

Revision ID: 7c4d9e2a1b60
Revises: 6b2f91d8c4a0
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "7c4d9e2a1b60"
down_revision: str | None = "6b2f91d8c4a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_sms_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("message_template_id", sa.Uuid(), nullable=False),
        sa.Column("template_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contact_type", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("selection_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="processing", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "contact_type IN ('initial', 'follow_up', 'second_follow_up', 'final_follow_up')",
            name="ck_t_sms_batch_contact_type",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'partially_completed')",
            name="ck_t_sms_batch_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_template_id"], ["t_message_template.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_t_sms_batch_tenant_idempotency"),
    )
    op.create_index(
        "ix_t_sms_batch_tenant_created", "t_sms_batch", ["tenant_id", "created_at"]
    )
    op.create_table(
        "t_sms_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("message_template_id", sa.Uuid(), nullable=False),
        sa.Column("template_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("template_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("template_body_snapshot", sa.Text(), nullable=False),
        sa.Column("rendered_body", sa.Text(), nullable=False),
        sa.Column("recipient_phone", sa.String(length=32), nullable=True),
        sa.Column("sender_snapshot", sa.String(length=100), nullable=False),
        sa.Column("contact_type", sa.String(length=30), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="unknown", nullable=False),
        sa.Column("provider_message_sid", sa.String(length=64), nullable=True),
        sa.Column("provider_error_code", sa.String(length=50), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "contact_type IN ('initial', 'follow_up', 'second_follow_up', 'final_follow_up')",
            name="ck_t_sms_message_contact_type",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'queued', 'sent', 'delivered', 'failed', 'undelivered', 'unknown', 'skipped')",
            name="ck_t_sms_message_status",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["t_sms_batch.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_template_id"], ["t_message_template.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "lead_id", name="uq_t_sms_message_batch_lead"),
        sa.UniqueConstraint("provider_message_sid", name="uq_t_sms_message_provider_sid"),
    )
    op.create_index("ix_t_sms_message_batch", "t_sms_message", ["batch_id"])
    op.create_index(
        "ix_t_sms_message_tenant_lead_created",
        "t_sms_message",
        ["tenant_id", "lead_id", "created_at"],
    )
    op.create_table(
        "t_sms_batch_recipient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("requested_lead_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("sms_message_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted', 'queued', 'sent', 'delivered', 'failed', 'undelivered', 'unknown', 'skipped')",
            name="ck_t_sms_batch_recipient_status",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["t_sms_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sms_message_id"], ["t_sms_message.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "requested_lead_id", name="uq_t_sms_batch_recipient_requested"),
        sa.UniqueConstraint("sms_message_id", name="uq_t_sms_batch_recipient_message"),
    )
    op.create_index(
        "ix_t_sms_batch_recipient_batch",
        "t_sms_batch_recipient",
        ["batch_id", "ordinal"],
    )
    op.create_table(
        "t_sms_message_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sms_message_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("provider_error_code", sa.String(length=50), nullable=True),
        sa.Column("safe_reason", sa.String(length=500), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sms_message_id"], ["t_sms_message.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_t_sms_message_event_message_time",
        "t_sms_message_event",
        ["sms_message_id", "occurred_at"],
    )
    op.add_column("t_lead_activity", sa.Column("sms_message_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_t_lead_activity_sms_message_id",
        "t_lead_activity",
        "t_sms_message",
        ["sms_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_t_lead_activity_sms_message_id", "t_lead_activity", ["sms_message_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_t_lead_activity_sms_message_id", "t_lead_activity", type_="unique")
    op.drop_constraint("fk_t_lead_activity_sms_message_id", "t_lead_activity", type_="foreignkey")
    op.drop_column("t_lead_activity", "sms_message_id")
    op.drop_index("ix_t_sms_message_event_message_time", table_name="t_sms_message_event")
    op.drop_table("t_sms_message_event")
    op.drop_index("ix_t_sms_batch_recipient_batch", table_name="t_sms_batch_recipient")
    op.drop_table("t_sms_batch_recipient")
    op.drop_index("ix_t_sms_message_tenant_lead_created", table_name="t_sms_message")
    op.drop_index("ix_t_sms_message_batch", table_name="t_sms_message")
    op.drop_table("t_sms_message")
    op.drop_index("ix_t_sms_batch_tenant_created", table_name="t_sms_batch")
    op.drop_table("t_sms_batch")
