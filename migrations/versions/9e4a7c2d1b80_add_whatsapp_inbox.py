"""add tenant scoped WhatsApp inbox

Revision ID: 9e4a7c2d1b80
Revises: 7c4d9e2a1b60
Create Date: 2026-10-22 12:00:00.000000
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "9e4a7c2d1b80"
down_revision: str | Sequence[str] | None = "7c4d9e2a1b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_ROLE_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
PERMISSIONS = {
    "messages.view": uuid.UUID("39000000-0000-0000-0000-000000000001"),
    "messages.send": uuid.UUID("39000000-0000-0000-0000-000000000002"),
}


def upgrade() -> None:
    op.create_table(
        "t_whatsapp_channel",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("twilio_account_sid", sa.String(length=64), nullable=False),
        sa.Column("sender_address", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "twilio_account_sid",
            "sender_address",
            name="uq_t_whatsapp_channel_account_sender",
        ),
    )
    op.create_index("ix_t_whatsapp_channel_tenant", "t_whatsapp_channel", ["tenant_id"])

    op.create_table(
        "t_whatsapp_conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("external_address", sa.String(length=32), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["t_whatsapp_channel.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["t_customer.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["t_lead.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_id",
            "external_address",
            name="uq_t_whatsapp_conversation_channel_contact",
        ),
    )
    op.create_index(
        "ix_t_whatsapp_conversation_tenant_activity",
        "t_whatsapp_conversation",
        ["tenant_id", "last_message_at"],
    )
    op.create_index(
        "ix_t_whatsapp_conversation_lead",
        "t_whatsapp_conversation",
        ["tenant_id", "lead_id"],
    )
    op.create_index(
        "ix_t_whatsapp_conversation_customer",
        "t_whatsapp_conversation",
        ["tenant_id", "customer_id"],
    )

    op.create_table(
        "t_whatsapp_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("media_metadata", sa.JSON(), nullable=False),
        sa.Column("sender_address", sa.String(length=32), nullable=False),
        sa.Column("recipient_address", sa.String(length=32), nullable=False),
        sa.Column("provider_message_sid", sa.String(length=64), nullable=True),
        sa.Column("provider_error_code", sa.String(length=50), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("submission_state", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_request_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_t_whatsapp_message_direction"),
        sa.CheckConstraint(
            "status IN ('received', 'accepted', 'queued', 'sending', 'sent', "
            "'delivered', 'read', 'failed', 'undelivered', 'unknown')",
            name="ck_t_whatsapp_message_status",
        ),
        sa.CheckConstraint(
            "submission_state IN ('received', 'processing', 'completed')",
            name="ck_t_whatsapp_message_submission_state",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["t_whatsapp_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["t_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_t_whatsapp_message_tenant_idempotency"
        ),
        sa.UniqueConstraint(
            "provider_message_sid", name="uq_t_whatsapp_message_provider_sid"
        ),
    )
    op.create_index(
        "ix_t_whatsapp_message_conversation_time",
        "t_whatsapp_message",
        ["conversation_id", "occurred_at", "id"],
    )

    op.create_table(
        "t_whatsapp_delivery_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("provider_message_sid", sa.String(length=64), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_error_code", sa.String(length=50), nullable=True),
        sa.Column("safe_reason", sa.String(length=500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('received', 'accepted', 'queued', 'sending', 'sent', "
            "'delivered', 'read', 'failed', 'undelivered', 'unknown')",
            name="ck_t_whatsapp_delivery_event_status",
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["t_whatsapp_channel.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["t_whatsapp_message.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["t_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_t_whatsapp_delivery_event_key"),
    )
    op.create_index(
        "ix_t_whatsapp_delivery_event_provider_sid",
        "t_whatsapp_delivery_event",
        ["tenant_id", "provider_message_sid", "occurred_at"],
    )

    op.create_table(
        "t_whatsapp_conversation_read",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("last_read_message_id", sa.Uuid(), nullable=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["t_whatsapp_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_read_message_id"], ["t_whatsapp_message.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["t_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", "user_id"),
    )

    op.add_column(
        "t_lead_activity",
        sa.Column("whatsapp_message_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_t_lead_activity_whatsapp_message_id",
        "t_lead_activity",
        "t_whatsapp_message",
        ["whatsapp_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_t_lead_activity_whatsapp_message_id",
        "t_lead_activity",
        ["whatsapp_message_id"],
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
            {"id": permission_id, "key": key, "description": key.replace(".", " ").title()}
            for key, permission_id in PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        role_permission_table,
        [
            {"role_id": OWNER_ROLE_ID, "permission_id": permission_id}
            for permission_id in PERMISSIONS.values()
        ],
    )


def downgrade() -> None:
    role_permission_table = sa.table(
        "t_role_permission", sa.column("permission_id", sa.Uuid())
    )
    permission_table = sa.table("t_permission", sa.column("id", sa.Uuid()))
    op.execute(
        role_permission_table.delete().where(
            role_permission_table.c.permission_id.in_(PERMISSIONS.values())
        )
    )
    op.execute(
        permission_table.delete().where(
            permission_table.c.id.in_(PERMISSIONS.values())
        )
    )
    op.drop_constraint(
        "uq_t_lead_activity_whatsapp_message_id", "t_lead_activity", type_="unique"
    )
    op.drop_constraint(
        "fk_t_lead_activity_whatsapp_message_id", "t_lead_activity", type_="foreignkey"
    )
    op.drop_column("t_lead_activity", "whatsapp_message_id")
    op.drop_table("t_whatsapp_conversation_read")
    op.drop_index(
        "ix_t_whatsapp_delivery_event_provider_sid",
        table_name="t_whatsapp_delivery_event",
    )
    op.drop_table("t_whatsapp_delivery_event")
    op.drop_index(
        "ix_t_whatsapp_message_conversation_time", table_name="t_whatsapp_message"
    )
    op.drop_table("t_whatsapp_message")
    op.drop_index(
        "ix_t_whatsapp_conversation_customer", table_name="t_whatsapp_conversation"
    )
    op.drop_index(
        "ix_t_whatsapp_conversation_lead", table_name="t_whatsapp_conversation"
    )
    op.drop_index(
        "ix_t_whatsapp_conversation_tenant_activity",
        table_name="t_whatsapp_conversation",
    )
    op.drop_table("t_whatsapp_conversation")
    op.drop_index("ix_t_whatsapp_channel_tenant", table_name="t_whatsapp_channel")
    op.drop_table("t_whatsapp_channel")
