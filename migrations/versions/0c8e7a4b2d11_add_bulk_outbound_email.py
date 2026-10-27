"""add bulk outbound email and contact stages

Revision ID: 0c8e7a4b2d11
Revises: f7c91e2a4b60
Create Date: 2026-10-15 18:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0c8e7a4b2d11"
down_revision: str | Sequence[str] | None = "f7c91e2a4b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "t_outbound_message_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("sender_account_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("contact_stage", sa.String(30), nullable=False),
        sa.Column("selection_mode", sa.String(20), nullable=False),
        sa.Column("selection_snapshot", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("selection_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), server_default="previewed", nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("selection_mode IN ('selected', 'all_matching')", name="ck_t_outbound_batch_selection_mode"),
        sa.CheckConstraint("contact_stage IN ('initial', 'follow_up', 'second_follow_up', 'final_follow_up')", name="ck_t_outbound_batch_stage"),
        sa.CheckConstraint("status IN ('previewed', 'queued', 'processing', 'completed', 'partially_completed', 'cancelled')", name="ck_t_outbound_batch_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sender_account_id"], ["t_sender_account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_id"], ["t_mail_template.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["template_version_id"], ["t_mail_template_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["t_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_t_outbound_batch_idempotency"),
    )
    op.create_index("ix_t_outbound_batch_tenant_created", "t_outbound_message_batch", ["tenant_id", "created_at"])
    op.create_table(
        "t_outbound_batch_recipient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("requested_lead_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=True),
        sa.Column("preview_status", sa.String(20), nullable=False),
        sa.Column("exclusion_code", sa.String(80), nullable=True),
        sa.Column("exclusion_reason", sa.String(500), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("preview_status IN ('eligible', 'blocked', 'conflict')", name="ck_t_outbound_batch_recipient_preview_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["t_outbound_message_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["preview_id"], ["t_outbound_message_preview.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_id"], ["t_outbound_message.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "requested_lead_id", name="uq_t_outbound_batch_recipient_lead"),
        sa.UniqueConstraint("message_id", name="uq_t_outbound_batch_recipient_message"),
    )
    op.create_index("ix_t_outbound_batch_recipient_batch", "t_outbound_batch_recipient", ["batch_id", "ordinal"])
    op.add_column("t_outbound_message", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.add_column("t_outbound_message", sa.Column("contact_stage", sa.String(30), server_default="initial", nullable=False))
    op.create_foreign_key("fk_t_outbound_message_batch", "t_outbound_message", "t_outbound_message_batch", ["batch_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_t_outbound_message_stage", "t_outbound_message", "contact_stage IN ('initial', 'follow_up', 'second_follow_up', 'final_follow_up')")
    op.create_index("uq_t_outbound_active_lead", "t_outbound_message", ["tenant_id", "lead_id"], unique=True, postgresql_where=sa.text("status IN ('queued', 'processing')"))
    op.add_column("t_lead_activity", sa.Column("contact_stage", sa.String(30), nullable=True))
    op.create_check_constraint("ck_t_lead_activity_contact_stage", "t_lead_activity", "contact_stage IS NULL OR contact_stage IN ('initial', 'follow_up', 'second_follow_up', 'final_follow_up')")


def downgrade() -> None:
    op.drop_constraint("ck_t_lead_activity_contact_stage", "t_lead_activity", type_="check")
    op.drop_column("t_lead_activity", "contact_stage")
    op.drop_index("uq_t_outbound_active_lead", table_name="t_outbound_message")
    op.drop_constraint("ck_t_outbound_message_stage", "t_outbound_message", type_="check")
    op.drop_constraint("fk_t_outbound_message_batch", "t_outbound_message", type_="foreignkey")
    op.drop_column("t_outbound_message", "contact_stage")
    op.drop_column("t_outbound_message", "batch_id")
    op.drop_index("ix_t_outbound_batch_recipient_batch", table_name="t_outbound_batch_recipient")
    op.drop_table("t_outbound_batch_recipient")
    op.drop_index("ix_t_outbound_batch_tenant_created", table_name="t_outbound_message_batch")
    op.drop_table("t_outbound_message_batch")
